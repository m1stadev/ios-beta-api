#!/usr/bin/env python3

from fastapi import FastAPI, HTTPException
from remotezip import RemoteZip
from typing import Optional

import aiohttp
import aiopath
import aiosqlite
import asyncio
import plistlib
import re
import time
import ujson
import wikitextparser as wtp

DB_PATH = aiopath.AsyncPath('betas.db')
DEVICE_REGEX = re.compile(r'(iPhone|AppleTV|iPad|iPod)[0-9]+,[0-9]+')

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


class WikiScraper:
    def __init__(
        self, session: aiohttp.ClientSession, db: aiosqlite.Connection
    ) -> None:
        self.session = session
        self.db = db
        self.http_semaphore = asyncio.Semaphore(
            100
        )  # Only allow 100 simultaneous HTTP requests
        self.pages = list()
        self.api = dict()
        self.ipsw_api = None

    async def get_pages(self, product_type: str) -> list:
        params = {
            'action': 'query',
            'list': 'search',
            'srsearch': f'Beta Firmware/{product_type}',
            'srwhat': 'title',
            'srlimit': 'max',
            'format': 'json',
        }

        async with self.http_semaphore:
            async with self.session.get(
                'https://www.theiphonewiki.com/w/api.php', params=params
            ) as resp:
                if resp.status != 200:
                    pass  # raise error
                else:
                    data = await resp.json()

        for page in data['query']['search']:
            if '.x' not in page['title']:  # We skip these pages:
                continue

            major_ver = int(page['title'].split('/')[2][:-2])
            min_major_ver = 9 if 'Apple TV' not in product_type else 7
            if major_ver <= min_major_ver:
                continue

            if page['title'] not in self.pages:
                self.pages.append(page['title'])

    async def parse_page(self, title: str) -> dict:  # i hate this entire function
        params = {
            'action': 'parse',
            'prop': 'wikitext',
            'page': title,
            'format': 'json',
            'formatversion': 2,
        }

        async with self.http_semaphore:
            async with self.session.get(
                'https://www.theiphonewiki.com/w/api.php', params=params
            ) as resp:
                if resp.status != 200:
                    pass  # raise error

                data = await resp.json()

        page_text = wtp.parse(data['parse']['wikitext'])
        for table in page_text.tables:
            template = table.data()[0]
            for firm in range(1, len(table.data())):
                firm_data = [x for x in table.data()[firm] if x is not None]
                devices = list()

                for device in wtp.parse(
                    firm_data[
                        next(
                            template.index(x)
                            for x in template
                            if any(i in x for i in ('Codename', 'Keys'))
                        )
                    ]
                ).wikilinks:
                    regex = DEVICE_REGEX.match(str(device.text))
                    if regex is not None:
                        devices.append(regex.group())

                firm = {'version': firm_data[0]}

                version = wtp.parse(firm['version'])
                if version.wikilinks:
                    for link in version.wikilinks:
                        if link.text is not None:
                            firm['version'] = firm['version'].replace(
                                str(link), link.text
                            )

                buildids = firm_data[1].split(
                    '   | '
                )  # Can't use the mediawiki parser for this, unfortunately
                if len(buildids) > 1:
                    buildids.pop(0)
                    buildids.pop(-1)
                    for b in range(len(buildids)):
                        buildids[b] = buildids[b].split(' = ')[-1][:-1]

                try:
                    ipsws = next(
                        wtp.parse(item).external_links
                        for item in firm_data
                        if wtp.parse(item).external_links
                    )
                except:  # No URLs for this firmware, skip
                    continue

                if not any(
                    ipsw.url.endswith('.ipsw') for ipsw in ipsws
                ):  # Only IPSW beta firmwares are scraped
                    continue

                ipsw_sizes = list()
                for word in [
                    x.replace(',', '').replace('\n', '')
                    for x in firm_data[-1].split(' ')
                ]:
                    if (word.isnumeric()) and (int(word) > 10):
                        ipsw_sizes.append(int(word))

                if len(ipsw_sizes) != len(
                    ipsws
                ):  # One or more IPSWs don't have filesizes, skip
                    continue

                for d in range(len(devices)):
                    firm_index = (
                        0
                        if ((len(devices) == 4) and (d in (0, 1)))
                        or ((len(devices) == 2) and (d == 0))
                        else 1
                    )
                    firm['buildid'] = buildids[0]
                    if len(buildids) > 1:
                        firm['buildid'] = buildids[firm_index]

                    firm['url'] = ipsws[0].url
                    firm['filesize'] = ipsw_sizes[0]

                    if len(ipsws) > 1:
                        firm['url'] = ipsws[firm_index].url
                        firm['filesize'] = ipsw_sizes[firm_index]

                    if len(firm.keys()) < 4:  # Incomplete firmware info, skipping
                        continue

                    if devices[d] not in self.api.keys():
                        self.api[devices[d]] = list()

                    if not any(
                        f['buildid'] == firm['buildid'] for f in self.api[devices[d]]
                    ):
                        self.api[devices[d]].append(firm)

    def _sync_get_manifest(self, firm: dict) -> Optional[bytes]:
        try:
            with RemoteZip(firm['url']) as ipsw:
                return ipsw.read(
                    next(f for f in ipsw.namelist() if 'BuildManifest' in f)
                )
        except:
            return None

    async def _get_manifest(self, firm: dict) -> Optional[bytes]:
        async with self.http_semaphore:
            async with self.session.get(
                f"{'/'.join(firm['url'].split('/')[:-1])}/BuildManifest.plist"
            ) as resp:
                if resp.status == 200:
                    return await resp.read()

        async with self.http_semaphore:
            async with self.session.get(firm['url']) as resp:
                if resp.status == 200:
                    return await asyncio.to_thread(self._sync_get_manifest, firm)

            return None

    async def check_firmware(self, device: dict, firm: dict) -> None:
        tss_request = dict(TATSU_REQUEST)
        tss_request['ApChipID'] = device['cpid']
        tss_request['ApBoardID'] = device['bdid']

        if 0x8900 <= device['cpid'] < 0x8960:  # 32-bit
            tss_request['@APTicket'] = True
        else:  # 64-bit
            tss_request['@ApImg4Ticket'] = True
            tss_request['ApSecurityMode'] = True
            tss_request['SepNonce'] = b'0'

        try:
            manifest = plistlib.loads(await self._get_manifest(firm))

            for i in manifest['BuildIdentities']:
                if 'RestoreBehavior' not in i['Info'].keys():
                    continue

                if (
                    i['Info']['DeviceClass'].casefold()
                    == device['boardconfig'].casefold()
                    and i['Info']['RestoreBehavior'] == 'Erase'
                ):
                    identity = i
                    break

            else:
                self.api[device['identifier']].remove(firm)
                return

        except:
            self.api[device['identifier']].remove(firm)
            return

        tss_request['UniqueBuildID'] = identity['UniqueBuildID']

        async with self.http_semaphore:
            async with self.session.post(
                TATSU_API,
                data=plistlib.dumps(tss_request),
                headers=TATSU_HEADERS,
                params=TATSU_PARAMS,
            ) as resp:
                firm_index = self.api[device['identifier']].index(firm)
                self.api[device['identifier']][firm_index]['signed'] = (
                    'MESSAGE=SUCCESS' in await resp.text()
                )

    async def check_device_signed_firmwares(self, identifier: str) -> None:
        if self.ipsw_api is None:
            async with self.http_semaphore:
                async with self.session.get('https://api.ipsw.me/v4/devices') as resp:
                    self.ipsw_api = await resp.json()

        device = next(
            d
            for d in self.ipsw_api
            if d['identifier'].casefold() == identifier.casefold()
        )

        await asyncio.gather(
            *[self.check_firmware(device, firm) for firm in self.api[identifier]]
        )

        await self.output_device_data(identifier)

    async def output_device_data(self, identifier: str) -> None:
        json_data = ujson.dumps(
            sorted(self.api[identifier], key=lambda firm: firm['buildid'], reverse=True)
        )
        async with self.db.execute(
            'SELECT * FROM betas WHERE identifier = ?', (identifier.lower(),)
        ) as cursor:
            if await cursor.fetchone() is not None:
                sql = 'UPDATE betas SET firmwares = ? WHERE identifier = ?'
            else:
                sql = 'INSERT INTO betas(firmwares, identifier) VALUES (?,?)'

        await self.db.execute(sql, (json_data, identifier.lower()))
        await self.db.commit()


app = FastAPI()


@app.on_event('startup')
async def app_startup():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            '''
        CREATE TABLE IF NOT EXISTS betas(
        identifier TEXT,
        firmwares JSON
        )
        '''
        )
        await db.commit()


@app.middleware("http")
async def add_process_time_header(request, call_next):
    start_time = time.time()
    response = await call_next(request)
    response.headers["X-Process-Time"] = str(f'{time.time() - start_time:0.4f} sec')
    return response


@app.get('/betas/{identifier}')
async def get_firmwares(identifier: str) -> str:
    async with aiosqlite.connect(DB_PATH) as db, db.execute(
        'SELECT firmwares FROM betas WHERE identifier = ?', (identifier.lower(),)
    ) as cursor:
        firmwares = await cursor.fetchone()

    try:
        return ujson.loads(firmwares[0])
    except:
        raise HTTPException(
            status_code=404, detail=f"No beta firmwares available for '{identifier}'."
        )


async def main() -> None:
    async with aiohttp.ClientSession() as session, aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            '''
        CREATE TABLE IF NOT EXISTS betas(
        identifier TEXT,
        firmwares JSON
        )
        '''
        )
        await db.commit()

        while True:
            scraper = WikiScraper(session, db)
            await asyncio.gather(
                *[
                    scraper.get_pages(product)
                    for product in (
                        'Apple TV',
                        'iPod touch',
                        'iPhone',
                        'iPad',
                        'iPad Air',
                        'iPad Pro',
                        'iPad Mini',
                    )
                ]
            )

            await asyncio.gather(*[scraper.parse_page(page) for page in scraper.pages])

            for device in scraper.api.keys():
                await scraper.check_device_signed_firmwares(device)


if __name__ == '__main__':
    asyncio.run(main())
