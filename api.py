#!/usr/bin/env python3

from remotezip import RemoteZip
from fastapi import FastAPI, HTTPException
from typing import Optional

import aiocache
import aiohttp
import aiosqlite
import asyncio
import plistlib
import time
import uvicorn

APPLEDB_URL = 'https://api.appledb.dev/main.json'

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


async def get_appledb_data(session: aiohttp.ClientSession) -> dict:
    async with session.get(APPLEDB_URL) as resp:
        if resp.status != 200:
            raise HTTPException(status_code=resp.status, detail=resp.reason)

        return await resp.json()


async def get_device_data(session: aiohttp.ClientSession, identifier: str) -> dict:
    data = await get_appledb_data(session)

    for device in data['device']:
        if any(identifier.casefold() == i.casefold() for i in device['identifier']):
            return device

    raise HTTPException(status_code=404, detail='Device not found')


async def get_firmware_data(session: aiohttp.ClientSession, os_type: str) -> list:
    data = await get_appledb_data(session)

    return [i for i in data['ios'] if i['osType'].casefold() == os_type.casefold()]


async def parse_firmware(
    session: aiohttp.ClientSession, device: dict, firmware: dict
) -> Optional[dict]:
    # Confirm this is a beta firmware
    if firmware['beta'] == False:
        return

    try:
        firm_dict = {
            'version': firmware['version'],
            'buildid': firmware['build'],
        }
    except StopIteration:  # This firmware doesn't support this device
        return

    # Confirm there's actually URLs available
    if 'sources' not in firmware.keys():
        return

    # Find some extra firmware data
    for source in firmware['sources']:
        if device['identifier'][0] not in source['deviceMap']:
            continue

        if source['type'] != 'ipsw':
            continue

        link = next(
            (
                i
                for i in source['links']
                if i['preferred'] == True and i['active'] == True
            ),
            None,
        )

        if link is None:
            try:
                link = next(i for i in source['links'] if i['active'] == True)
            except StopIteration:
                continue

        # TODO: Find a more efficient way to do this
        async with session.head(link['url']) as resp:
            if resp.status != 200:
                continue

        # Add firmware URL
        firm_dict['url'] = link['url']

        # Add different hashes
        # if 'sha1' in source['hashes'].keys():
        #    firm_dict['sha1sum'] = source['hashes']['sha1']

        # if 'md5' in source['hashes'].keys():
        #    firm_dict['md5sum'] = source['hashes']['md5']

        # if 'sha2-256' in source['hashes'].keys():
        #    firm_dict['sha256sum'] = source['hashes']['sha2-256']

        # Add file info
        firm_dict['filesize'] = source['size']

        break

    else:  # No valid data found, skip this firmware
        return

    try:
        manifest = plistlib.loads(await get_manifest(session, firm_dict))
    except:  # Failed to download/parse manifest, can't check signing status
        return

    signed = await check_firmware(session, device, firm_dict, manifest)
    if signed is None:
        return

    firm_dict['signed'] = signed
    return firm_dict


def _sync_get_manifest(firmware: dict) -> Optional[bytes]:
    try:
        with RemoteZip(firmware['url']) as ipsw:
            return ipsw.read(next(f for f in ipsw.namelist() if 'BuildManifest' in f))
    except:
        return None


async def get_manifest(
    session: aiohttp.ClientSession, firmware: dict
) -> Optional[bytes]:
    async with HTTP_SEMAPHORE:
        async with session.get(
            f"{'/'.join(firmware['url'].split('/')[:-1])}/BuildManifest.plist"
        ) as resp:
            if resp.status == 200:
                return await resp.read()

        async with session.get(firmware['url']) as resp:
            if resp.status == 200:
                return await asyncio.to_thread(_sync_get_manifest, firmware)

        return None


async def check_firmware(
    session: aiohttp.ClientSession, device: dict, firmware: dict, manifest: dict
) -> Optional[bool]:
    for i in manifest['BuildIdentities']:
        if (
            i['Info']['DeviceClass'].casefold() == device['board'][0].casefold()
            and i['Info']['RestoreBehavior'] == 'Erase'
        ):
            identity = i
            break
    else:
        return

    tss_request = {
        'ApChipID': int(identity['ApChipID'], 16),
        'ApBoardID': int(identity['ApBoardID'], 16),
        'ApECID': 1,  # ECID 0 will make Tatsu mistakenly report some unsigned firmwares as signed
        'ApSecurityDomain': 1,
        'ApNonce': b'0',
        'ApProductionMode': True,
        'UniqueBuildID': identity['UniqueBuildID'],
    }

    if 0x8900 <= tss_request['ApChipID'] < 0x8960:  # 32-bit
        tss_request['@APTicket'] = True
    else:  # 64-bit
        tss_request['@ApImg4Ticket'] = True
        tss_request['ApSecurityMode'] = True
        tss_request['SepNonce'] = b'0'

    async with HTTP_SEMAPHORE:
        async with session.post(
            TATSU_API,
            data=plistlib.dumps(tss_request),
            headers=TATSU_HEADERS,
            params=TATSU_PARAMS,
        ) as resp:
            return 'MESSAGE=SUCCESS' in await resp.text()


app = FastAPI()


@app.middleware('http')
async def add_process_time_header(request, call_next):
    start_time = time.time()
    response = await call_next(request)
    response.headers['X-Process-Time'] = str(f'{time.time() - start_time:0.4f} sec')
    return response


@app.get('/betas/{identifier}')
async def get_beta_firmwares(identifier: str) -> str:
    async with aiohttp.ClientSession() as session:
        device = await get_device_data(session, identifier)
        data = await asyncio.gather(
            *[
                parse_firmware(session, device, firm)
                for firm in await get_firmware_data(session=session, os_type='iOS')
            ]
        )

        return [i for i in data if i is not None][::-1]


if __name__ == '__main__':
    uvicorn.run(app='__main__:app', workers=2)
