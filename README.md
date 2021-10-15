# iOS Beta Firmware API
This is a python script that scrapes information on beta iOS firmwares from [The iPhone Wiki](https://www.theiphonewiki.com/), fetches the signing status of the firmwares, then serves the information as a Flask app, in a format similar to [IPSW.me](https://ipswdownloads.docs.apiary.io/)'s API.

## Requirements
- A computer running macOS or Linux
- An Internet connection
- [tsschecker](https://github.com/1Conan/tsschecker)
- Pip dependencies:
    - `pip3 install -r requirements.txt`

## Usage
`curl https://api.m1sta.xyz/betas/<identifier>` (updated every hour)

## Support
For support, open an [issue](https://github.com/m1stadev/ios-beta-api/issues/new), or join my [Discord server](https://m1sta.xyz/discord).
