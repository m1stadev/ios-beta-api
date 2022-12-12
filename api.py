#!/usr/bin/env python3

import asyncio
import plistlib
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import aiohttp
import aiosqlite
import uvicorn
from fastapi import FastAPI, HTTPException
from remotezip import RemoteZip

TATSU_API = 'http://gs.apple.com/TSS/controller'
TATSU_HEADERS = {
    'Cache-Control': 'no-cache',
    'Content-Type': 'text/xml; charset="utf-8"',
    'User-Agent': 'InetURL/1.0',
}
TATSU_PARAMS = {'action': 2}
TATSU_REQUEST = {
    'ApChipID': 0,
    'ApBoardID': 0,
    'ApECID': 1,  # ECID 0 will make Tatsu mistakenly report some unsigned firmwares as signed
    'ApSecurityDomain': 1,
    'ApNonce': b'0',
    'ApProductionMode': True,
    'UniqueBuildID': bytes(),
}

HTTP_SEMAPHORE = asyncio.Semaphore(100)


class AppleDB:
    URL = 'https://api.appledb.dev/main.json'

    def __init__(self, db: aiosqlite.Connection, session: aiohttp.ClientSession):
        self._db = db
        self._session = session
        self._data = {'ios': [], 'device': []}

    async def _scrape_device(self, device: dict) -> None:
        async with self._db.execute(
            'SELECT * FROM devices WHERE identifier = ? and boardconfig = ?',
            (device['key'], device['board'][0]),
        ) as cursor:
            if await cursor.fetchone() is not None:
                return

        await self._db.execute(
            'INSERT INTO devices(identifier, boardconfig) VALUES(?, ?)',
            (device['key'], device['board'][0]),
        )
        await self._db.commit()

    async def _scrape_firmware(self, firmware: dict) -> None:
        for source in firmware['sources']:
            if source['type'] != 'ipsw':
                continue

            async with self._db.execute(
                'SELECT * FROM firmwares WHERE buildid = ? and devices = ?',
                (firmware['build'], ', '.join(source['deviceMap'])),
            ) as cursor:
                if await cursor.fetchone() is not None:
                    continue

            for link in source['links']:
                async with self._session.head(link['url'], timeout=5) as resp:
                    if resp.status != 200:
                        continue

                    await self._db.execute(
                        'INSERT INTO firmwares(version, buildid, url, size, devices) VALUES(?, ?, ?, ?, ?)',
                        (
                            firmware['version'],
                            firmware['build'],
                            link['url'],
                            source['size'],
                            ', '.join(source['deviceMap']),
                        ),
                    )
                    await self._db.commit()

                    # Scrape information from BuildManifest needed to check signing status
                    for _ in range(5):
                        try:
                            manifest = await get_manifest(self._session, link['url'])
                            if manifest is not None:
                                break
                        except:
                            continue

                    if manifest is None:
                        continue

                    manifest = plistlib.loads(manifest)
                    for identity in manifest['BuildIdentities']:
                        if 'RestoreBehavior' not in identity['Info'].keys():
                            continue

                        if identity['Info']['RestoreBehavior'] == 'Erase':
                            continue

                        async with self._db.execute(
                            'SELECT * FROM buildmanifest WHERE boardconfig = ? AND buildid = ?',
                            (
                                identity['Info']['DeviceClass'],
                                identity['Info']['BuildNumber'],
                            ),
                        ) as cursor:
                            if await cursor.fetchone() is not None:
                                continue

                        await self._db.execute(
                            'INSERT INTO buildmanifest(boardconfig, buildid, chip_id, board_id, unique_buildid) VALUES(?, ?, ?, ?, ?)',
                            (
                                identity['Info']['DeviceClass'],
                                identity['Info']['BuildNumber'],
                                int(identity['ApChipID'], 16),
                                int(identity['ApBoardID'], 16),
                                identity['UniqueBuildID'],
                            ),
                        )
                        await self._db.commit()

            else:
                continue

    async def _bound_scrape_firmware(self, firmware: dict) -> None:
        async with HTTP_SEMAPHORE:
            await self._scrape_firmware(firmware)

    async def scrape_data(self) -> None:
        async with self._session.get(self.URL) as resp:
            if resp.status != 200:
                raise HTTPException(status_code=resp.status, detail=resp.reason)

            data = await resp.json()

        firmwares = []
        for firm in data['ios']:
            if firm in self._data['ios']:
                continue

            if firm['osStr'] not in ('iPadOS', 'iOS', 'Apple TV Software', 'tvOS'):
                continue

            if 'sources' not in firm.keys():
                continue

            if firm['beta'] == False:
                continue

            firmwares.append(firm)

        devices = []
        for device in data['device']:
            if device in self._data['device']:
                continue

            if 'arch' not in device.keys() or 'arm' not in device['arch']:
                continue

            if device['type'] not in (
                'iPhone',
                'iPad',
                'iPad mini',
                'iPad Air',
                'iPad Pro',
                'Apple TV',
            ):
                continue

            if len(device['board']) == 0:
                continue

            devices.append(device)

        firmwares.sort(key=lambda x: x['build'], reverse=True)

        async with asyncio.TaskGroup() as tg:
            for device in devices:
                tg.create_task(self._scrape_device(device))

            for firmware in firmwares:
                tg.create_task(self._bound_scrape_firmware(firmware))

            while len(tg._tasks) > 0:
                await asyncio.sleep(1)

        self._data = data


def _sync_get_manifest(url: str) -> Optional[bytes]:
    try:
        with RemoteZip(url) as ipsw:
            return ipsw.read(next(f for f in ipsw.namelist() if 'BuildManifest' in f))
    except:
        return None


async def get_manifest(session: aiohttp.ClientSession, url: str) -> Optional[bytes]:
    manifest_url = urlparse(url)
    manifest_url = manifest_url._replace(
        path=str(Path(manifest_url.path).parents[0] / 'BuildManifest.plist')
    ).geturl()

    async with session.get(manifest_url) as resp:
        if resp.status == 200:
            return await resp.read()

        else:
            return await asyncio.to_thread(_sync_get_manifest, url)


async def _is_firmware_signed(
    db: aiosqlite.Connection,
    session: aiohttp.ClientSession,
    identifier: str,
    firmware: dict,
) -> dict:
    async with db.execute(
        'SELECT boardconfig FROM devices WHERE identifier LIKE ?',
        (f'%{identifier}%',),
    ) as cursor:
        try:
            boardconfig = (await cursor.fetchone())[0]
        except TypeError:
            return

    async with db.execute(
        'SELECT chip_id, board_id, unique_buildid FROM buildmanifest WHERE boardconfig LIKE ? AND buildid = ?',
        (f'%{boardconfig}%', firmware['buildid']),
    ) as cursor:
        try:
            chip_id, board_id, unique_buildid = await cursor.fetchone()
        except TypeError:
            # delete firmware so it can be parsed again later
            await db.execute(
                'DELETE FROM firmwares WHERE buildid = ? AND devices LIKE ?',
                (firmware['buildid'], f'%{identifier}%'),
            )
            await db.commit()

            return

    tss_request = {
        'ApChipID': chip_id,
        'ApBoardID': board_id,
        'ApECID': 1,  # ECID 0 will make Tatsu mistakenly report some unsigned firmwares as signed
        'ApSecurityDomain': 1,
        'ApNonce': b'0',
        'ApProductionMode': True,
        'UniqueBuildID': unique_buildid,
    }

    if 0x8900 <= tss_request['ApChipID'] < 0x8960:  # 32-bit
        tss_request['@APTicket'] = True
    else:  # 64-bit
        tss_request['@ApImg4Ticket'] = True
        tss_request['ApSecurityMode'] = True
        tss_request['SepNonce'] = b'0'

    async with session.post(
        TATSU_API,
        data=plistlib.dumps(tss_request),
        headers=TATSU_HEADERS,
        params=TATSU_PARAMS,
    ) as resp:
        firmware['signed'] = 'MESSAGE=SUCCESS' in await resp.text()

    return firmware


async def is_firmware_signed(
    db: aiosqlite.Connection,
    session: aiohttp.ClientSession,
    identifier: str,
    firmware: dict,
) -> bool:
    async with HTTP_SEMAPHORE:
        return await _is_firmware_signed(db, session, identifier, firmware)


app = FastAPI()


async def main() -> None:
    async with aiosqlite.connect('betas.db') as db, aiohttp.ClientSession() as session:
        await db.execute(
            '''
            CREATE TABLE IF NOT EXISTS firmwares(
            version TEXT,
            buildid TEXT,
            url TEXT,
            size INTEGER,
            devices TEXT
            )
            '''
        )
        await db.commit()

        await db.execute(
            '''
            CREATE TABLE IF NOT EXISTS devices(
            identifier TEXT,
            boardconfig TEXT
            )
            '''
        )
        await db.commit()

        await db.execute(
            '''
            CREATE TABLE IF NOT EXISTS buildmanifest(
            boardconfig TEXT,
            buildid TEXT,
            chip_id INTEGER,
            board_id INTEGER,
            unique_buildid BLOB
            )
            '''
        )
        await db.commit()

        appledb = AppleDB(db, session)
        while True:
            await appledb.scrape_data()
            await asyncio.sleep(60)


@app.on_event('startup')
async def startup_event():
    asyncio.create_task(main())


@app.middleware('http')
async def add_process_time_header(request, call_next):
    start_time = time.time()
    response = await call_next(request)
    response.headers['X-Process-Time'] = str(f'{time.time() - start_time:0.4f} sec')
    return response


@app.get('/betas/{identifier}')
async def get_beta_firmwares(identifier: str) -> str:
    async with aiosqlite.connect('betas.db') as db:
        async with db.execute(
            'SELECT version, buildid, size, url FROM firmwares WHERE devices LIKE ?',
            (f'%{identifier}%',),
        ) as cursor:
            firmwares = []
            for firm_tuple in await cursor.fetchall():
                firmwares.append(
                    {
                        'version': firm_tuple[0],
                        'buildid': firm_tuple[1],
                        'filesize': firm_tuple[2],
                        'url': firm_tuple[3],
                    }
                )

        async with aiohttp.ClientSession() as session, asyncio.TaskGroup() as tg:
            tasks = [
                tg.create_task(is_firmware_signed(db, session, identifier, firmware))
                for firmware in firmwares
            ]

        firmwares = [task.result() for task in tasks if task.result() is not None]
        return sorted(firmwares, key=lambda x: x['buildid'], reverse=True)


if __name__ == '__main__':
    uvicorn.run(app='__main__:app')
