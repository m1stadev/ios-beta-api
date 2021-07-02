# Beta Firmware API
This is a python script that scrapes information on beta iOS firmwares from [The iPhone Wiki](https://www.theiphonewiki.com/), fetches the signing status of the firmwares, then writes the data out as JSON, in a format similar to [IPSW.me](https://ipswdownloads.docs.apiary.io/)'s API. I'm also hosting this API for others to use (updated every 3 hours):
`https://api.m1sta.xyz/betas/{identifier}`.

## Requirements
- A computer running macOS or Linux
- An Internet connection
- [tsschecker](https://github.com/1Conan/tsschecker)
- Pip dependencies:
    - `pip3 install -r requirements.txt`

## Support
For support, open an [issue](https://github.com/m1stadev/beta-firmware-API/issues/new), or join my [Discord server](https://m1sta.xyz/discord).
