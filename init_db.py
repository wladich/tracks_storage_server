#!/usr/bin/env python
# coding: utf-8
import sys
import os
import config
import psycopg2


if __name__ == '__main__':
    connection = psycopg2.connect(**config.db)
    connection.set_session(autocommit=True)
    schema = open(os.path.join(os.path.dirname(sys.argv[0]), 'init.sql')).read().split(';')
    cursor = connection.cursor()
    for st in schema:
        cursor.execute(st + ';')

