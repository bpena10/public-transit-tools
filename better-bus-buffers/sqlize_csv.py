################################################################################
# sqlize_csv.py, originally written by Luitien Pan
# Last updated 30 November 2017 by Melinda Morang, Esri
################################################################################
'''Copyright 2017 Esri
   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at
       http://www.apache.org/licenses/LICENSE-2.0
   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.'''
################################################################################
# Imports the CSV-formatted GTFS information into a SQLite database file.
# Handles data conversion, table creation, and indexing transparently.
#
# If you specify multiple GTFS datasets, this merges them.  In order to avoid
# collisions between identifiers that are supposed to be dataset-unique, I
# prepend an agency label to each *_id field value.  This label comes from the
# last component of the corresponding GTFS_DIR* path.  So for example, if I
# keep my CTA data in
#   /home/luitien/gtfs/cta/*.txt
# then stop_id 1518 gets stored and manipulated as "cta:1518".
#
# In line with this, *don't* put your CSV files like so:
#   [...]/cta/data/*.txt
#   [...]/metra/data/*.txt
# and so on, because then they'll all be labelled as ``data''.

import csv
import datetime
import itertools
import os
import re
import sqlite3
import sys
import arcpy

import hms
import BBB_SharedFunctions

ispy3 = sys.version_info >= (3, 0)

csv_fnames = ["stops.txt", "calendar.txt", "calendar_dates.txt", "stop_times.txt", "trips.txt", "routes.txt", "frequencies.txt"]

sql_types = {
        str :   "TEXT" ,
        float : "REAL" ,
        int :   "INTEGER" ,
    }
# Each subdictionary specifies the columns for the named sql table.
# The format is:
#   tbl_name : { col_name : (datatype, is_required) }
# where is_required is True for columns required by the GTFS
#                   and names the default value otherwise.
sql_schema = {
        "stops" : {
                "stop_id" :     (str, True) ,
                "stop_code" :   (str, "NULL") ,
                "stop_name" :   (str, True) ,
                "stop_desc" :   (str, "NULL") ,
                "stop_lat" :    (float, True) ,
                "stop_lon" :    (float, True) ,
                "zone_id" :     (str, "NULL") ,
                "stop_url" :    (str, "NULL") ,
                "location_type" : (int, "NULL") ,
                "parent_station" : (str, "NULL") ,
            } ,
        "calendar" : {
                "service_id" :  (str, True) ,
                "monday" :      (int, True) ,
                "tuesday" :         (int, True) ,
                "wednesday" :       (int, True) ,
                "thursday" :        (int, True) ,
                "friday" :      (int, True) ,
                "saturday" :        (int, True) ,
                "sunday" :      (int, True) ,
                "start_date" :      (str, True) ,
                "end_date" :        (str, True) ,
            } ,
        "calendar_dates" : {
                "service_id" :  (str, True) ,
                "date" :      (str, True) ,
                "exception_type" :         (int, True) ,
            } ,
        "stop_times" : {
                "trip_id" :     (str, True) ,
                "arrival_time" :    (float, True) ,
                "departure_time" :  (float, True) ,
                "stop_id" :         (str, True) ,
                "stop_sequence" :   (int, True) ,
                "stop_headsign" :   (str, "NULL") ,
                "pickup_type" :     (int, "0") ,
                "drop_off_type" :   (int, "0") ,
                "shape_dist_traveled" : (float, "NULL")
            } ,
        "trips" : {
                "route_id" :    (str, True) ,
                "service_id" :  (str, True) ,
                "trip_id" :     (str, True) ,
                "trip_headsign" :   (str, "NULL") ,
                "trip_short_name" :     (str, "NULL") ,
                "direction_id" : (int, "NULL") ,
                "block_id" :    (str, "NULL") ,
                "shape_id" :    (str, "NULL") ,
            } ,
        "routes" : {
                "route_id" :    (str, True),
                "agency_id" :  (str, "NULL"),
                "route_short_name": (str, "NULL"),
                "route_long_name":  (str, "NULL"),
                "route_desc":   (str, "NULL"),
                "route_type":   (int, True),
                "route_url":    (str, "NULL"),
                "route_color":  (str, "NULL"),
                "route_text_color": (str, "NULL"),
            },
        "frequencies" : {
                "trip_id" :     (str, True),
                "start_time" :  (float, True),
                "end_time" :    (float, True),
                "headway_secs" :    (int, True)
            }
    }

db = None


def connect(dbname):
    global db
    db = sqlite3.connect(dbname)


def check_time_str(s):
    '''Check that the string s is a valid clock time of the form HH:MM:SS.'''
    if not re.match('^-?\d?\d:\d\d:\d\d$', s):
        return False
    return True


def make_add_agency_labels(service, columns):
    '''Make a function that adds ${service}_* labels to the *_id columns
    of a row of data.'''
    service = re.sub("[^A-Za-z0-9]", "", service)
    # Figure out which columns need labelling:
    s = set()
    for idx,field in enumerate(columns):
        if field.endswith("_id") and field != "direction_id":
            s.add(idx)
    # ... and here's the function:
    def add_labels(row):
        ret = list(row)
        for idx in s:
            ret[idx] = "%s:%s" % (service, row[idx].strip())
        return tuple(ret)
    return add_labels


def make_remove_extra_fields(tablename, columns):
    '''Make a function that removes extraneous columns from the CSV rows.
    E.g.: the CTA dataset has things like stops.wheelchair_boarding and
    trips.direction that aren't in the spec.'''
    orig_num_fields = len(columns)
    # Identify the extraneous columns:
    cols = [ ]
    tbl = sql_schema[tablename]
    for idx,field in enumerate(columns):
        if field not in tbl:
            cols.append(idx)
    cols.reverse()
    # ... and here's the function:
    def drop_fields(in_row):
        out_row = list(in_row)
        # Check that row was the correct length in the first place.
        if len(out_row) != orig_num_fields:
            msg = "GTFS table %s contains at least one row with the wrong number of fields. Fields: %s; Row: %s" % (tablename, columns, str(in_row))
            arcpy.AddError(msg)
            raise BBB_SharedFunctions.CustomError
        # Remove the row entries for the extraneous columns
        for idx in cols:
            out_row.pop(idx)
        return tuple(out_row)
    return drop_fields


def check_for_required_fields(tablename, columns, dataset):
    '''Check that the GTFS file has the required fields'''
    for col in sql_schema[tablename]:
        if sql_schema[tablename][col][1] == True:
            if not col in columns:
                msg = "GTFS file " + tablename + ".txt in dataset " + dataset + " is missing required field '" + col + "'. Failed to SQLize GTFS data"
                arcpy.AddError(msg)
                raise BBB_SharedFunctions.CustomError


def smarter_convert_times(rows, col_names, fname, GTFSdir, time_columns=('arrival_time', 'departure_time')):
    '''Parses time fields according to the column name.  Accepts HMS or numeric
    times, converting to seconds-since-midnight.'''

    time_column_idxs = [col_names.index(x)  for x in time_columns]
    def convert_time_columns(row):
        out_row = row[:]    # copy
        for idx in time_column_idxs:
            field = row[idx].strip()
            if check_time_str(field):
                out_row[idx] = hms.str2sec(field)
            elif field == '':
                msg = "GTFS dataset " + GTFSdir + " contains empty \
values for arrival_time or departure_time in stop_times.txt.  Although the \
GTFS spec allows empty values for these fields, this toolbox \
requires exact time values for all stops.  You will not be able to use this \
dataset for your analysis."
                arcpy.AddError(msg)
                raise BBB_SharedFunctions.CustomError
            else:
                try:
                    out_row[idx] = float (field)
                except ValueError:
                    msg = 'Column "' + col_names[idx] + '" in file ' + os.path.join(GTFSdir, fname) + ' has an invalid value: ' + field + '.'
                    arcpy.AddError(msg)
                    raise BBB_SharedFunctions.CustomError
        return out_row
    if ispy3:
        return map(convert_time_columns, rows)
    else:
        return itertools.imap(convert_time_columns, rows)


def check_date_fields(rows, col_names, tablename, fname):
    '''Ensure date fields are the in the correct YYYYMMDD format before adding them to the SQL table'''
    def check_date_cols(row):
        if tablename == "calendar":
            date_cols = ["start_date", "end_date"]
        elif tablename == "calendar_dates":
            date_cols = ["date"]
        date_column_idxs = [col_names.index(x) for x in date_cols]
        for idx in date_column_idxs:
            date = row[idx]
            try:
                datetime.datetime.strptime(date, '%Y%m%d')
            except ValueError:
                msg ='Column "' + col_names[idx] + '" in file ' + fname + ' has an invalid value: ' + date + '. \
Date fields must be in YYYYMMDD format. Please check the date field formatting in calendar.txt and calendar_dates.txt.'
                arcpy.AddError(msg)
                raise BBB_SharedFunctions.CustomError
        return row
    if ispy3:
        return map(check_date_cols, rows)
    else:
        return itertools.imap(check_date_cols, rows)


def check_latlon_fields(rows, col_names, fname):
    '''Ensure lat/lon fields are valid'''
    def check_latlon_cols(row):
        stop_id = row[col_names.index("stop_id")]
        stop_lat = row[col_names.index("stop_lat")]
        stop_lon = row[col_names.index("stop_lon")]
        try:
            stop_lat_float = float(stop_lat)
        except ValueError:
            msg = 'stop_id "%s" in %s contains an invalid non-numerical value \
for the stop_lat field: "%s". Please double-check all lat/lon values in your \
stops.txt file.' % (stop_id, fname, stop_lat)
            arcpy.AddError(msg)
            raise BBB_SharedFunctions.CustomError
        try:
            stop_lon_float = float(stop_lon)
        except ValueError:
            msg = 'stop_id "%s" in %s contains an invalid non-numerical value \
for the stop_lon field: "%s". Please double-check all lat/lon values in your \
stops.txt file.' % (stop_id, fname, stop_lon)
            arcpy.AddError(msg)
            raise BBB_SharedFunctions.CustomError
        if not (-90.0 <= stop_lat_float <= 90.0):
            msg = 'stop_id "%s" in %s contains an invalid value outside the \
range (-90, 90) the stop_lat field: "%s". stop_lat values must be in valid WGS 84 \
coordinates.  Please double-check all lat/lon values in your stops.txt file.\
' % (stop_id, fname, stop_lat)
            arcpy.AddError(msg)
            raise BBB_SharedFunctions.CustomError
        if not (-180.0 <= stop_lon_float <= 180.0):
            msg = 'stop_id "%s" in %s contains an invalid value outside the \
range (-180, 180) the stop_lon field: "%s". stop_lon values must be in valid WGS 84 \
coordinates.  Please double-check all lat/lon values in your stops.txt file.\
' % (stop_id, fname, stop_lon)
            arcpy.AddError(msg)
            raise BBB_SharedFunctions.CustomError
        return row
    if ispy3:
        return map(check_latlon_cols, rows)
    else:
        return itertools.imap(check_latlon_cols, rows)


def column_specs(tablename):
    '''Turns the sql_schema python datastructure above into the appropriate
    column specs for a CREATE TABLE statement.  Used in create_table().'''
    tblspec = sql_schema[tablename]
    lines = [ "id   INTEGER PRIMARY KEY" ]
    for col_name in tblspec:
        col_type,required = tblspec[col_name]
        data_type = sql_types[col_type]
        if required is True:
            defaults_str = ""
        else:
            defaults_str = " DEFAULT %s" % required
        lines.append ("%s\t%s%s" % (col_name, data_type, defaults_str))
    return " ,\n".join (lines)


def create_table(tablename):
    db.execute("DROP TABLE IF EXISTS %s;" % tablename)
    create_stmt = "CREATE TABLE %s (%s);" % (tablename, column_specs (tablename))
    db.execute(create_stmt)
    db.commit()


def handle_file(fname, service_label):
    '''Creates and populates a table for the given CSV file.'''

    if fname.endswith(".txt"):
        tablename = fname[:-4]
    else:
        tablename = fname
    tablename = os.path.basename(tablename)

    #-- Read in everything from the CSV table
    if ispy3:
        f = open(fname, encoding="utf-8-sig")
    else:
        f = open(fname)
    reader = csv.reader(f)
    # Put everything in utf-8 to handle BOMs and weird characters.
    # Eliminate blank rows (extra newlines) while we're at it.
    if ispy3:
        reader = ([x.strip() for x in r] for r in reader if len(r) > 0)
    else:
        reader = ([x.decode('utf-8-sig').strip() for x in r] for r in reader if len(r) > 0)


    # First row is column names:
    columns = [name.strip() for name in next(reader)]

    #-- Do some data validity checking and reformatting
    # Check that all required fields are present
    check_for_required_fields(tablename, columns, service_label)
    # This is the only file with HH:MM:SS time strings. Convert to seconds since midnight.
    if tablename == "stop_times":
        rows = smarter_convert_times(reader, columns, fname, service_label)
    elif tablename == "frequencies":
        rows = smarter_convert_times(reader, columns, fname, service_label, ('start_time', 'end_time'))
    # Make sure date fields are in YYYYMMDD format
    elif tablename in ["calendar", "calendar_dates"]:
        rows = check_date_fields(reader, columns, tablename, fname)
    # Make sure lat/lon values are valid
    elif tablename == "stops":
        rows = check_latlon_fields(reader, columns, fname)
    # Otherwise just leave them as they are
    else:
        rows = reader
    # Prepare functions for adding agency labels and filtering out unrequired columns
    labeller = make_add_agency_labels(service_label, columns)
    columns_filter = make_remove_extra_fields(tablename, columns)
    # Remove unnecessary columns
    columns = columns_filter(columns)
    # Add agency labels for merged datasets
    if ispy3:
        rows = map(labeller, rows)
    else:
        rows = itertools.imap(labeller, rows)
    # Remove data from columns that aren't in the spec
    if ispy3:
        rows = map(columns_filter, rows)
    else:
        rows = itertools.imap(columns_filter, rows)

    # Add to the SQL table
    values_placeholders = ["?"] * len(columns)
    cur = db.cursor()
    cur.executemany("INSERT INTO %s (%s) VALUES (%s);" %
                        (tablename,
                        ",".join(columns),
                        ",".join(values_placeholders))
                        , rows)
    db.commit()
    cur.close()
    f.close()


def handle_agency(gtfs_dir):
    '''Parses the relevant parts of an agency's GTFS CSV files into
    the sqlite database. Returns a list of error messages from some basic
    GTFS dataset validation'''

    try:
        csvs_withPaths = []
        # Create a dataset label
        label = os.path.basename(os.path.normpath(gtfs_dir))

        # Verify that the required files are present
        missing_files = []
        has_a_calendar = 0
        for fname in csv_fnames:
            fname2 = os.path.join(gtfs_dir, fname)
            if os.path.exists(fname2):
                csvs_withPaths.append(fname2)
                # We must have at least one of calendar or calendar_dates
                if fname in ["calendar_dates.txt", "calendar.txt"]:
                    has_a_calendar = 1
            else:
                # These files aren't required
                if fname not in ["calendar.txt", "calendar_dates.txt", "frequencies.txt"]:
                    missing_files.append(fname)
        if not has_a_calendar:
            missing_files.append("calendar.txt or calendar_dates.txt")
        if missing_files:
            arcpy.AddError(u"GTFS dataset %s is missing files required for \
this tool: %s" % (label, str(missing_files)))
            raise BBB_SharedFunctions.CustomError

        # Sqlize each GTFS file
        for fname2 in csvs_withPaths:
            handle_file(fname2, label)

    except UnicodeDecodeError:
        arcpy.AddError(u"Unicode decoding of GTFS dataset %s failed. Please \
ensure that your GTFS files have the proper utf-8 encoding required by the GTFS \
specification." % label)
        raise BBB_SharedFunctions.CustomError


def create_indices():
    cur = db.cursor()
    cur.execute("CREATE INDEX trips_index_serviceIDs ON trips (service_id);")
    cur.execute("CREATE INDEX trips_index_routeIDs ON trips (route_id, direction_id);")
    cur.execute("CREATE INDEX stops_index_stopIDs ON stops (stop_id);")
    cur.execute("CREATE INDEX stopTimes_index_stopIdsDep ON stop_times (stop_id, departure_time);")
    cur.execute("CREATE INDEX stopTimes_index_stopIdsArr ON stop_times (stop_id, arrival_time);")
    cur.execute("CREATE INDEX stopTimes_index_tripIdsDep ON stop_times (trip_id, departure_time);")
    cur.execute("CREATE INDEX stopTimes_index_tripIdsArr ON stop_times (trip_id, arrival_time);")
    cur.execute("CREATE INDEX stopTimes_index_tripIdsSeq ON stop_times (trip_id, stop_sequence);")
    cur.execute("CREATE INDEX calendar_index_serviceIds ON calendar (service_id);")
    cur.execute("CREATE INDEX calendardates_index_date ON calendar_dates (date);")
    db.commit()
    cur.close()

def metadata():
    db.execute("DROP TABLE IF EXISTS metadata;")
    db.execute("CREATE TABLE metadata (key TEXT, value TEXT);")
    db.execute("""INSERT INTO metadata (key, value) VALUES ("sql_format", "1");""")
    db.execute("""INSERT INTO metadata (key, value) VALUES ("sqlize_csv", "$Id: sqlize_csv.py 59 2013-05-13 14:41:37Z luitien $");""")
    db.execute("""INSERT INTO metadata (key, value) VALUES ("timestamp", ?);""", (datetime.datetime.now().isoformat(),))
    db.commit()

def check_nonoverlapping_dateranges():
    '''Check for non-overlapping date ranges in calendar.txt to prevent
    double-counting in analyses that use generic weekdays.'''
    # Function by Melinda Morang, Esri

    # Only do this if we have a calendar table from calendar.txt.
    c = db.cursor()
    GetTblNamesStmt = "SELECT name FROM sqlite_master WHERE type='table' AND name='calendar';"
    c.execute(GetTblNamesStmt)
    tblnames = c.fetchall()
    if tblnames:

        # Check for non-overlapping date ranges to prevent double-counting.
        serviceidlist = []
        startdatedict = {}
        enddatedict = {}
        overlapwarning = ""
        nonoverlappingsids = []
        # Find all the service_ids.
        serviceidfetch = '''
            SELECT service_id, start_date, end_date FROM calendar
            ;'''
        c.execute(serviceidfetch)
        ids = c.fetchall()
        for id in ids:
            # Add to the list of service_ids
            serviceidlist.append(id[0])
            startdatedict[id[0]] = id[1]
            enddatedict[id[0]] = id[2]
        # Check for non-overlapping date ranges.
        for sid in serviceidlist:
            for eid in serviceidlist:
                if startdatedict[sid] > enddatedict[eid]:
                    nonoverlappingsids.append([sid, eid])
                if len(nonoverlappingsids) >= 10:
                    break
            if len(nonoverlappingsids) >= 10:
                    break
        if nonoverlappingsids:
            overlapwarning = u"Warning! Your calendar.txt file(s) contain(s) \
non-overlapping date ranges. As a result, your analysis might double \
count the number of trips available if you are analyzing a generic weekday \
instead of a specific date.  This is especially likely if the \
non-overlapping pairs are in the same GTFS dataset.  Please check the date \
ranges in your calendar.txt file(s). See the User's Guide for further \
assistance.  Date ranges do not overlap in the following pairs of service_ids: "
            if len(nonoverlappingsids) == 10:
                overlapwarning += "(Showing the first 10 non-overlaps) "
            overlapwarning += str(nonoverlappingsids)

    # Close up the SQL file.
    c.close()

    return overlapwarning
