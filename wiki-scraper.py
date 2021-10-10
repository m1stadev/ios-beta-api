#!/usr/bin/env python3

from concurrent.futures import ThreadPoolExecutor
from mwclient import Site
import json
import os
import platform
import plistlib
import re
import requests
import remotezip
import shutil
import subprocess
import sys
import tempfile
import time
import wikitextparser as wtp


class Firmware:
    def __init__(self, firm):
        self.version = firm[0]
        self.buildid = firm[1]
        self.url = firm[2]
        self.size = firm[3]
        self.raw = firm

class BetaScraper:
    def __init__(self, site):
        self.site = site
        self.api = dict()

    def build_api(self, filters):
        device_regex = re.compile('(iPhone|AppleTV|iPad|iPod)[0-9]+,[0-9]+')
        for result in self.site.search('Beta Firmware/'):
            if any(x in result['title'] for x in filters) and '.x' in result['title']:
                wiki_page = wtp.parse(self.site.pages[result['title']].text())
                for t in range(len(wiki_page.tables)):
                    for ver in range(1, len(wiki_page.tables[t].data())):
                        try:
                            devices = wtp.parse(wiki_page.tables[t].data()[ver][2]).wikilinks
                        except: # Some parsing issue I haven't fixed yet, just skip the firmware
                            continue

                        for d in range(len(devices)):
                            try:
                                devices[d] = device_regex.search(str(devices[d])).group()
                            except AttributeError: # Sometimes baseband versions get included in the list of devices for some reason, so we'll skip
                                devices.pop(d)

                        template = [wiki_page.tables[t].data()[0].index(x) for x in wiki_page.tables[t].data()[0] if any(i == x for i in ('Version', 'Build', 'Download URL', 'File Size'))]
                        if len(wiki_page.tables[t].data()[0]) > len([x for x in wiki_page.tables[t].data()[ver] if x is not None]): # Similar issue to above ^
                            for x in range(len(template)):
                                if template[x] > 1:
                                    template[x] = template[x] - 1

                        firm = [x for x in wiki_page.tables[t].data()[ver] if wiki_page.tables[t].data()[ver].index(x) in template]
                        if len(firm) != 4: # Incomplete firmware info, skipping
                            continue

                        firm = Firmware(firm)
                        for device in devices:
                            if device not in self.api.keys():
                                self.api[device] = list()

                            if not any(f['buildid'] == firm.buildid for f in self.api[device]):
                                self.api[device].append({
                                    'version': firm.version,
                                    'buildid': firm.buildid,
                                    'url': firm.url,
                                    'size': firm.size,
                                })

    def scrape_firm(self, url): #TODO: Update this function to only check signing status + remove tsschecker dependency
        if any(domain in url for domain in ('developer.apple.com', 'adcdownload.apple.com')): # Inaccessible domains
            return

        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                with remotezip.RemoteZip(url) as ipsw:
                    manifest = next(f for f in ipsw.namelist() if 'Manifest' in f)
                    ipsw.extract(manifest, tmpdir)
            except:
                return

            with open('/'.join((tmpdir, manifest)), 'rb') as f:
                bm = plistlib.load(f)

            api = requests.get(f"https://api.ipsw.me/v4/device/{bm['SupportedProductTypes'][0]}").json()

            args = (
                'tsschecker',
                '-d',
                bm['SupportedProductTypes'][0],
                '-B',
                api['boards'][0]['boardconfig'],
                '-m',
                f'{tmpdir}/{manifest}'
            )

            tsschecker = subprocess.run(args, stdout=subprocess.PIPE, universal_newlines=True)

        for device in bm['SupportedProductTypes']:
            if device not in self.api.keys():
                self.api[device] = list()

            self.api[device].append({
                'version': bm['ProductVersion'],
                'buildid': bm['ProductBuildVersion'],
                'url': url,
                'signed': True if 'IS being signed!' in tsschecker.stdout else False
            })

    def write_api(self, path):
        if os.path.exists(path):
            shutil.rmtree(path)

        os.mkdir(path)
        for device in self.api.keys():
            with open(f'{path}/{device}', 'w') as f:
                json.dump(sorted(self.api[device], key=lambda firm: firm['buildid'], reverse=True), f)

def main():
    if platform.system() == 'Windows':
        sys.exit('[ERROR] Windows is not supported. Exiting.')

    if shutil.which('tsschecker') is None:
        sys.exit('[ERROR] tsschecker is not installed. Exiting.')

    start_time = time.time()
    device_types = ('Apple TV', 'iPad', 'iPad Air', 'iPad Pro', 'iPad Mini', 'iPhone', 'iPod touch')
    scraper = BetaScraper(Site('www.theiphonewiki.com'))

    print('[1] Scraping The iPhone Wiki...')
    pages = scraper.build_api(device_types)
    print('[4] Writing out API...')
    scraper.write_api('betas')
    exit()

    print('[3] Grabbing signing status (this will take a while, please wait)...')
    with ThreadPoolExecutor() as executor:
        for url in urls:
            executor.submit(scraper.scrape_firm, url)

    print('[4] Writing out API...')
    scraper.write_api('betas')

    print(f'Done! Took {round(time.time() - start_time)}s.')

if __name__ == "__main__":
    main()
