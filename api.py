#!/usr/bin/env python3

from flask import Flask
import sqlite3


api = Flask('iOS Beta Firmware API')

@api.route('/betas/<identifier>', methods=['GET'])
def get_firmwares(identifier: str) -> str:
    db = sqlite3.connect('betas.db')
    cursor = db.cursor()

    cursor.execute('SELECT firmwares FROM betas WHERE identifier = ?', (identifier.lower(),))
    firmwares = cursor.fetchone()
    db.close()

    if firmwares is not None:
        return api.response_class(response=firmwares, mimetype='application/json')
    else:
        return api.response_class(status=404)


if __name__ == '__main__':
    api.run()
