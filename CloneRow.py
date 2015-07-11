#! /usr/local/bin/python
""" Python module for cloning a MYSQL row from one host to another """

import MySQLdb as mdb
import ConfigParser, time, datetime, argparse, os, stat, sys
from DictDiffer import DictDiffer

class CloneRow(object):
    """ CloneRow constructor, doesn't take any args """
    # TODO:
    #   - re-arrange file, private methods when not in main, etc
    #   - pretty logging
    #   - better documentation with params etc

    def __init__(self):
        self.config = ConfigParser.ConfigParser(allow_no_value=True)
        self.config.readfp(open('CloneRow.cfg'))
        self.source = {
            'alias': None,
            'connection': None
        }
        self.target = {
            'alias': None,
            'connection': None
        }
        self.database = {
            'table': None,
            'column': None,
            'filter': None,
            'ignore_columns': []
        }
        self.target_insert = False

    def parse_cla(self):
        """ parse command line arguments """
        parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
        aliases = [section for section in self.config.sections() if 'host.' in section]
        parser.add_argument(
            '--schema_only', '-s',
            help='diff schema only, do not consider data (column and filter not required)',
            action='store_true',
            default=False
        )
        parser.add_argument(
            'source_alias',
            help='source host alias (for host.* config section)',
            choices=[alias[5:] for alias in aliases]
        )
        parser.add_argument(
            'target_alias',
            help='target host alias (for host.* section)',
            choices=[alias[5:] for alias in aliases]
        )
        parser.add_argument('table', help='table to consider: select from <table>')
        parser.add_argument(
            'column',
            nargs='?',
            help='column to consider: select from table where <column>'
        )
        parser.add_argument(
            'filter',
            nargs='?',
            help='value to filter column: select from table where column = <filter>'
        )
        args = parser.parse_args()
        if not args.schema_only and (args.column is None or args.filter is None):
            print '\n', \
                'column & filter arguments must be supplied unless running with --schema_only/-s\n'
            parser.print_help()
            sys.exit(0)
        self.source['alias'] = 'host.' + args.source_alias
        self.target['alias'] = 'host.' + args.target_alias
        if self.source['alias'] == self.target['alias']:
            self.error('source and target alias are identical')
        self.database['table'] = args.table
        self.database['column'] = args.column
        self.database['filter'] = args.filter
        self.get_table_config(self.database['table'])
        self.config.set('clone_row', 'unload_filepath', self.get_unload_filepath())
        self.config.set('clone_row', 'schema_only', str(args.schema_only))

    def get_table_config(self, table):
        """ get table specific config items, if any """
        table_section = 'table.' + table
        if not self.config.has_section(table_section):
            print 'get_table_config: no table specific config defined'
            return
        try:
            self.database['ignore_columns'] = self.config.get(
                table_section, 'ignore_columns'
            ).rsplit(',')
            ignore_string = 'The following columns will be ignored: '
            for column in self.database['ignore_columns']:
                ignore_string += column + ' '
            print ignore_string
        except ConfigParser.NoOptionError:
            print 'get_table_config: no ignore_columns for ' + self.database['table']
            return

    def error(self, message):
        """ wrapper for raising errors that does housekeeping too """
        self.housekeep()
        raise Exception('FATAL: ' + message)

    def connect(self, host_alias):
        """ connect to a mysql database, returning a MySQLdb.Connection object """
        hostname = self.config.get(host_alias, 'hostname')
        user = self.config.get(host_alias, 'username')
        port = self.config.getint(host_alias, 'port')
        database = self.config.get(host_alias, 'database')
        password = self.config.get(host_alias, 'password')
        if password is not None:
            con = mdb.connect(
                host=hostname,
                user=user,
                db=database,
                port=port,
                passwd=password
            )
        else:
            con = mdb.connect(
                host=hostname,
                user=user,
                db=database,
                port=port
            )
        version = con.get_server_info()
        print 'Connected to {0}@{1}:{2} - Database version : {3} '.format(
            user, host_alias[5:], database, version
        )
        return con

    def get_row(self, con):
        """ Run a select query (MYSQLdb.Connection.query)
            returning a dict including column headers.
            Should always return a single row.
        """
        if self.config.getboolean('clone_row', 'schema_only'):
            # if we're only doing schema diffs we don't care about columns or filters
            select_sql = 'select * from {0} limit 1'.format(self.database['table'])
        else:
            # we're not using cursors here because we want the nice object with column headers
            select_sql = 'select * from {0} where {1} = {2}'.format(
                self.database['table'],
                self.database['column'],
                self.quote_sql_param(con.escape_string(self.database['filter']))
            )
        con.query(select_sql)
        res = con.store_result()
        # we should only _ever_ be playing with one row, per host, at a time
        if res.num_rows() == 0:
            return None
        if res.num_rows() != 1:
            self.error('get_row: Only one row expected -- cannot clone on multiple rows!')
        row = res.fetch_row(how=1)
        return dict(row[0])

    @classmethod
    def check_config_chmod(cls):
        """ make sure the read permissions of CloneRow.cfg are set correctly """
        chmod = oct(stat.S_IMODE(os.stat('CloneRow.cfg').st_mode))
        if chmod != '0600':
            print 'CloneRow.cfg needs to be secure\n\nchmod 0600 CloneRow.cfg\n\n'
            raise Exception('FATAL: CloneRow.cfg is insecure')

    @classmethod
    def find_deltas(cls, source_row, target_row):
        """ use DictDiffer to find what's different between target and source """
        delta = DictDiffer(source_row, target_row)
        return {
            'new_columns_in_source': delta.added(),
            'new_columns_in_target': delta.removed(),
            'delta_columns': delta.changed(),
            'unchanged_columns': delta.unchanged()
        }

    @classmethod
    def quote_sql_param(cls, sql_param):
        """ 'quote' a param if necessary, else return it as is.
            param should be escaped (Connection.escape_string) before it's passed in
            we should use cursors and parameterisation where possible, but sometimes we
            need to use the Connection.query method, so this is necessary
        """
        if isinstance(sql_param, str) or isinstance(sql_param, datetime.datetime):
            return '\'{0}\''.format(sql_param)
        else:
            # doesn't need quoting
            return sql_param

    def get_unload_filepath(self):
        """ return the unload filepath for us to use for backups and sql dumps """
        unload_file = self.config.get('clone_row', 'unload_dir')
        unload_file += '/{0}-{1}-{2}-{3}'.format(
            self.database['table'],
            self.database['column'],
            self.database['filter'],
            int(round(time.time() * 1000))
        )
        return unload_file

    def get_column_sql(self, con, column):
        """ return sql to add or drop a given column from the table we're working on """
        drop_sql = 'alter table `{0}` drop column `{1}`;'.format(self.database['table'], column)
        con.query('show fields from {0} where field = \'{1}\''.format(
            self.database['table'],
            column
        ))
        res = con.store_result()
        if res.num_rows() != 1:
            self.error('get_column_sql: only one row expected!')
        column_info = dict(res.fetch_row(how=1)[0])
        not_null = '' if column_info['Null'] == 'YES' else ' not null'
        default = '' if column_info['Default'] is None else ' default ' + column_info['Default']
        add_sql = 'alter table `{0}` add column `{1}` {2}{3}{4};'.format(
            self.database['table'],
            column,
            column_info['Type'],
            default,
            not_null
        )
        return {
            'add_sql': add_sql,
            'drop_sql': drop_sql
        }

    def show_ddl_updates(self, mode, deltas):
        """ display SQL statements to adjust database for column deltas
            mode: (source|target)
            con: database connection (for source or target)
            table: table we're working on
            deltas: column differences
        """
        working_db = self.source['alias'] if mode == 'source' else self.target['alias']
        other_db = self.target['alias'] if mode == 'source' else self.source['alias']
        con = self.source['connection'] \
            if working_db == self.source['alias'] else self.target['connection']

        for column in deltas:
            print '\n|----------------------|column: {0}|----------------------|\n'.format(column)
            print '\'{0}\' exists in the {1} database but not in {2}\n'.format(
                column, working_db, other_db
            )
            info = self.get_column_sql(con, column)
            print 'ADD: to add column \'{0}\' to {1}, run the following SQL on {2}:\n'.format(
                column, other_db, other_db)
            print info['add_sql'], '\n'
            print 'DROP: to drop column \'{0}\' from {1}, run the following SQL on {2}:\n'.format(
                column, working_db, working_db)
            print info['drop_sql'], '\n'
            print '|-----------------------{0}-----------------------|'.format(
                '-' * len('column: ' + column)
            )

    def dump_update_sql(self, cursor):
        """ dump the last executed update statement to a file """
        # naughty naughty, accessing a protected member.. show me a better way
        sql = cursor._last_executed
        sql_file = self.config.get('clone_row', 'unload_filepath') + '.sql'
        with open(sql_file, "w") as outfile:
            outfile.write(sql)
        print 'update sql is available for inspection at {0} on this machine'.format(sql_file)

    def update_target(self, source_row, deltas):
        """ update the data in the target database with differences from source """
        if not len(deltas):
            return
        cur = self.target['connection'].cursor()
        update_sql = None
        update_params = []
        # generate update sql for everything in the deltas
        for column in deltas:
            if column in self.database['ignore_columns']:
                continue
            if not update_sql:
                update_sql = 'update {0} set {1} = %s'.format(self.database['table'], column)
            else:
                update_sql += ', {0} = %s'.format(column)
            update_params.append(source_row[column])
        update_sql += ' where {0} = %s'.format(self.database['column'])
        update_params.append(self.database['filter'])
        # run the update
        cur.execute(update_sql, tuple(update_params))
        self.dump_update_sql(cur)
        if self.target['connection'].affected_rows() != 1:
            self.target['connection'].rollback()
            cur.close()
            self.error('update_target: expected to update a single row')
        # don't commit anything until all updates have gone in ok
        cur.close()
        self.target['connection'].commit()
        return

    def unload_target(self):
        """ unload the row we're working on from the target_db in case we ruin it """
        print 'backing up target row..'
        cur = self.target['connection'].cursor()
        unload_file = self.config.get('clone_row', 'unload_filepath') + '.backup'
        cur.execute('select * into outfile \'{0}\' from {1} where {2} = %s'.format(
            unload_file,
            self.database['table'],
            self.database['column']
        ), (self.database['filter'], ))
        if self.target['connection'].affected_rows() != 1:
            self.error('unload_target: unable to verify unload file')
        print 'backup file can be found at {0} on {1}'.format(unload_file, self.target['alias'])
        return unload_file

    def restore_target(self, unload_file):
        """ restore data unloaded from unload_target """
        cur = self.target['connection'].cursor()
        delete_sql = 'delete from {0} where {1} = %s'.format(
            self.database['table'],
            self.database['column']
        )
        cur.execute(delete_sql, (self.database['filter'], ))
        if self.target['connection'].affected_rows() != 1:
            cur.close()
            self.target['connection'].rollback()
            self.error('restore_target: expected to delete only one row')
        if self.target_insert:
            print 'just deleting as target row was inserted from scratch'
            cur.close()
            self.target['connection'].commit()
            return
        restore_sql = 'load data infile \'{0}\' into table {1}'.format(
            unload_file,
            self.database['table']
        )
        cur.execute(restore_sql)
        if self.target['connection'].affected_rows() != 1:
            cur.close()
            self.target['connection'].rollback()
            self.error('restore_target: expected to load only one row')
        cur.close()
        self.target['connection'].commit()

    def print_delta_columns(self, deltas):
        """ helper function to print out columns which will be updated """
        print '\n\n|----------------------------------------------------------|'
        print 'The following columns will be updated on ' + self.target['alias']
        for column in deltas:
            if column in self.database['ignore_columns']:
                continue
            print '\t- ' + column
        print '|----------------------------------------------------------|\n'

    @classmethod
    def user_happy(cls):
        """ Give the user a chance to restore from backup easily beforer we terminate """
        print 'Row has been cloned successfully..'
        print 'Type \'r\' to (r)estore from backup, anything else to termiate'
        descision = raw_input()
        if descision == 'r':
            print 'restoring from backup..'
            return False
        else:
            return True

    def minimal_insert(self):
        """ insert as little data as possible into the target database
            this will allow us to reselect and continue as normal if
            the row doesn't exist at all
        """
        # TODO - we could find all the columns that require default values
        #        and spam defaults of the appropriate datatype in there..
        cur = self.target['connection'].cursor()
        insert_sql = 'insert into {0} ({1}) values (%s)'.format(
            self.database['table'],
            self.database['column']
        )
        cur.execute(insert_sql, (self.database['filter'],))
        if self.target['connection'].affected_rows() != 1:
            cur.close()
            self.error('somehow we\'ve inserted multiple rows')
        # now we have a row, we can return it as usual
        return self.get_row(self.target['connection'])

    def print_restore_sql(self, backup):
        """ tell the user how to rollback by hand after script has run """
        restore_manual_sql = '  begin;\n'
        restore_manual_sql += '  delete from {0} where {1} = {2};\n'.format(
            self.database['table'],
            self.database['column'],
            self.quote_sql_param(self.database['filter'])
        )
        restore_manual_sql += '  -- the above should have delete one row, '
        restore_manual_sql += 'if not, run: rollback;\n'
        restore_manual_sql += '  load data infile \'{0}\' into table {1};\n'.format(
            backup, self.database['table']
        )
        restore_manual_sql += '  commit;\n'
        print '\n|------------------------------------------------------------|\n'
        print ' To rollback manually, run the following sql on {0}\n'.format(
            self.target['alias'])
        print restore_manual_sql
        print '|------------------------------------------------------------|'
        return

    def housekeep(self):
        """ close connections / whatever else """
        print 'housekeeping..'
        if self.source['connection'] is not None:
            self.source['connection'].close()
        if self.target['connection'] is not None:
            self.target['connection'].close()

    def main(self):
        """ main method """
        self.check_config_chmod()
        self.parse_cla()
        self.source['connection'] = self.connect(self.source['alias'])
        self.target['connection'] = self.connect(self.target['alias'])
        # we don't want mysql commit stuff unless we've okay'd it
        self.target['connection'].autocommit(False)
        print 'getting source row..'
        source_row = self.get_row(self.source['connection'])
        if source_row is None:
            self.error('row does not exist in source database')
        print 'getting target row..'
        target_row = self.get_row(self.target['connection'])
        if target_row is None:
            print 'row does not exist at all in target, running a minimal insert..'
            self.target_insert = True
            target_row = self.minimal_insert()
        print 'finding deltas..'
        deltas = self.find_deltas(source_row, target_row)
        self.show_ddl_updates('source', deltas['new_columns_in_source'])
        self.show_ddl_updates('target', deltas['new_columns_in_target'])
        if self.config.getboolean('clone_row', 'schema_only'):
            # we're done if only diffing schema
            print 'operation completed successfully, have a fantastic day'
            self.housekeep()
            sys.exit(0)
        if not len(deltas['delta_columns']):
            print '\ndata is identical in target and source, nothing to do..'
            self.housekeep()
            return True
        if set(deltas['delta_columns']).issubset(set(self.database['ignore_columns'])):
            print '\nall deltas ignored - [table.{0}]:ignore_columns, nothing to do..'.format(
                self.database['table']
            )
            self.housekeep()
            return True
        self.print_delta_columns(deltas['delta_columns'])
        backup = self.unload_target()
        self.update_target(source_row, deltas['delta_columns'])
        if not self.user_happy():
            self.restore_target(backup)
        else:
            print 'operation completed successfully, have a fantastic day'
            self.print_restore_sql(backup)
        self.housekeep()

DOLLY = CloneRow()
DOLLY.main()
