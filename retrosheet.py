#!/usr/bin/env python
import urllib
import os
import subprocess
import ConfigParser
import threading
import Queue
import zipfile
import glob
import tempfile
import re
import time
import sqlalchemy
import sys
import csv

THREADS = 20
RETROSHEET_URL = "http://www.retrosheet.org/game.htm"
CHADWICK = "/usr/local/bin/"

config = ConfigParser.ConfigParser()
config.readfp(open('db.ini'))

try:
    ENGINE = config.get('database', 'engine')
    HOST = config.get('database', 'host')
    DATABASE = config.get('database', 'database')
    
    USER = None if not config.has_option('database', 'user') else config.get('database', 'user')
    SCHEMA = None if not config.has_option('database', 'schema') else config.get('database', 'schema')
    PASSWORD = None if not config.has_option('database', 'password') else config.get('database', 'password')
    
except ConfigParser.NoOptionError:
    print 'Need to define engine, user, password, host, and database parameters'
    raise SystemExit

if USER and PASSWORD: string = '%s://%s:%s@%s/%s' % (ENGINE, USER, PASSWORD, HOST, DATABASE)
elif USER: string = '%s://%s@%s/%s' % (ENGINE, USER, HOST, DATABASE)
else:  string = '%s://%s/%s' % (ENGINE, HOST, DATABASE)

try:
    db = sqlalchemy.create_engine(string)
    conn = db.connect()
except:
    print 'Cannot connect to database'
    raise SystemExit


if SCHEMA: conn.execute('SET search_path TO %s' % SCHEMA)


class Parser(threading.Thread):
    def __init__(self, queue):
        threading.Thread.__init__(self)
        self.queue = queue

    def run(self):
        while 1:
            try: year = self.queue.get_nowait()
            except Queue.Empty: break;

            cmd = "%s/cwevent -q -n -f 0-96 -x 0-60 -y %d %d*.EV* > events-%d.csv" % (CHADWICK, year, year, year)
            subprocess.call(cmd, shell=True)
            cmd = "%s/cwgame -q -n -f 0-83 -y %d %d*.EV* > games-%d.csv" % (CHADWICK, year, year, year)
            subprocess.call(cmd, shell=True)

            for file in glob.glob("%d*" % year): os.remove(file)


class Fetcher(threading.Thread):
    def __init__(self, queue, path):
        threading.Thread.__init__(self)
        self.queue = queue
        self.path = path

    def run(self):
        while 1:
            try: url = self.queue.get_nowait()
            except Queue.Empty: break;

            f = "%s/%s" % (self.path, os.path.basename(url))
            urllib.urlretrieve(url, f)

            if (zipfile.is_zipfile(f)):
                zip = zipfile.ZipFile(f, "r")
                zip.extractall(self.path)

            os.remove(f)

start = time.time()
path = tempfile.mkdtemp()
os.chdir(path)

print "fetching retrosheet files..."
queue = Queue.Queue()
pattern = r'(\d{4}?)eve\.zip'
for match in re.finditer(pattern, urllib.urlopen(RETROSHEET_URL).read(), re.S):
    url = 'http://www.retrosheet.org/events/%seve.zip' % match.group(1)
    queue.put(url)

threads = []
for i in range(THREADS):
    t = Fetcher(queue, path)
    t.start()
    threads.append(t)

# finish fetching before processing events into CSV
for thread in threads: thread.join()

print "processing game files..."
queue = Queue.Queue()

years = []
threads = []
for file in glob.glob("%s/*.EV*" % path):
    year = re.search(r"^\d{4}", os.path.basename(file)).group(0)
    if year not in years:
        queue.put(int(year))
        years.append(year)

for i in range(THREADS):
    t = Parser(queue)
    t.start()
    threads.append(t)

# finishing processing games before processing rosters
for thread in threads: thread.join()

print "processing rosters..."
for file in glob.glob("*.ROS"):
    f = open(file, "r")

    team, year = re.findall(r"(^\w{3})(\d{4}).+?$", os.path.basename(file))[0]
    for line in f.readlines():

        if line.strip() == "": continue

        info = line.strip().replace('"', '').split(",")

        info.insert(0, team)
        info.insert(0, year)

        # wacky '\x1a' ASCII characters, probably some better way of handling this
        if len(info) == 3: continue

        # ROSTERS table has nine columns, let's fill it out
        if len(info) < 9:
            for i in range (9 - len(info)): info.append(None)

        sql = "INSERT INTO rosters VALUES (%s)" % ", ".join(["%s"] * len(info))
        conn.execute(sql, info)

print "processing teams..."
for file in glob.glob("TEAM*"):
    f = open(file, "r")

    try: year = re.findall(r"^TEAM(\d{4})$", os.path.basename(file))[0]
    except: continue

    for line in f.readlines():

        if line.strip() == "": continue

        info = line.strip().replace('"', '').split(",")
        info.insert(0, year)

        if len(info) < 5: continue

        sql = "INSERT INTO teams VALUES (%s)" % ", ".join(["%s"] * len(info))
        conn.execute(sql, info)


for file in glob.glob("events-*.csv"):
    print "processing %s" % file
    reader = csv.reader(open(file))
    headers = reader.next()
    for row in reader:
        sql = 'INSERT INTO events(%s) VALUES(%s)' % (','.join(headers), ','.join(['%s'] * len(headers)))
        conn.execute(sql, row)

for file in glob.glob("games-*.csv"):
    print "processing %s" % file
    reader = csv.reader(open(file))
    headers = reader.next()
    for row in reader:
        sql = 'INSERT INTO games(%s) VALUES(%s)' % (','.join(headers), ','.join(['%s'] * len(headers)))
        conn.execute(sql, row)

# cleanup!
for file in glob.glob("%s/*" % path): os.remove(file)

os.rmdir(path)
conn.close()

elapsed = (time.time() - start)
print "%d seconds!" % elapsed
