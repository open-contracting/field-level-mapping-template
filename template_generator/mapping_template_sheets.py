import csv
import jsonref
import re
import requests
from collections import OrderedDict
from operator import itemgetter

from google.colab import auth
from oauth2client.client import GoogleCredentials
from ocdskit.schema import get_schema_fields
from ocdsextensionregistry import ProfileBuilder
from pydrive.auth import GoogleAuth
from pydrive.drive import GoogleDrive


class MappingTemplateSheetsGenerator(object):

    extension_field = 'extension'

    def __init__(self, strings=None, schema_url=None, extensions_info=None, mapping_sheet='mapping-sheet.csv', lang='en'):
        self.strings = strings
        self.schema_url = schema_url
        self.mapping_sheet_file = mapping_sheet
        self.lang = lang
        self.extensions_info = extensions_info
        self.field_extensions = {}
        
        # read extension names per path from mapping-sheet
        with open(self.mapping_sheet_file, 'r') as f: 
            reader = csv.DictReader(f, dialect='excel')
            row = next(reader)
            for row in reader:
                if self.extension_field in row.keys():
                    self.field_extensions[row['path']] = row[self.extension_field]

    def authenticate_pydrive(self):
        auth.authenticate_user()
        gauth = GoogleAuth()
        gauth.credentials = GoogleCredentials.get_application_default()
        drive = GoogleDrive(gauth)

        return drive

    def get_string(self, key):
        return self.strings[key][self.lang]

    def get_patched_schema(self):
        schema_response = requests.get(self.schema_url)
        schema = schema_response.json()

        builder = ProfileBuilder(None, self.extensions_info.extension_urls)
        schema = builder.patched_release_schema(schema=schema, extension_field=self.extension_field)
        return jsonref.JsonRef.replace_refs(schema, base_uri=self.schema_url)

    def generate_mapping_sheets(self):
        # get schema
        schema = self.get_patched_schema()

        # create list for each mapping sheet
        sheets = {x: [] for x in (
            'general',
            'planning',
            'tender',
            'awards',
            'contracts',
            'implementation',
            'parties',
            'schema',
            'schema_extensions'
            )}

        extension_rows = {x: OrderedDict() for x in (
            'general',
            'planning',
            'tender',
            'awards',
            'contracts',
            'implementation',
            'parties'
            )}


        # use the mapping sheet to load the schema and schema_extensions tabs
        header = []
        with open(self.mapping_sheet_file) as csvfile:
            readme = csv.reader(csvfile, dialect='excel')
            header = next(readme)

            sheets['schema'].append(header[:-1])

            for row in readme:
                url = row[7]
                url = url.replace('1.1-dev', '1.1.5')
                row[7] = url
                if row[10]:
                    sheets['schema_extensions'].append(row)
                else:
                    sheets['schema'].append(row[:-1])

        # move the extension column to the beginning
        sheets['schema_extensions'] = [ row[-1:] + row[1:-1] for row in sheets['schema_extensions'] ]

        # sort the Extension Schemas by extension, stage and path
        sheets['schema_extensions'].sort(key=itemgetter(0,1))

        # add header
        sheets['schema_extensions'] = [header[-1:] + header[1:-1]] + sheets['schema_extensions']

        # create list for fields to repeat on parties sheet
        parties_rows = []

        # create list for organization references to add to parties sheet
        org_refs = []
        org_refs_extensions = OrderedDict();

        # set default depth for row grouping in Google Sheets
        depth = 0

        # regular expression to find links in schema descriptions
        INLINE_LINK_RE = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')

        # remove links from top-level schema description
        links = dict(INLINE_LINK_RE.findall(schema['description']))
        for key, link in links.items():
            schema['description'] = schema['description'].replace('[' + key + '](' + link + ')', key)

        # add header rows to each sheet
        headers = ['column_headers',
                depth,
                self.get_string('path_header'),
                self.get_string('title_header'),
                self.get_string('description_header'),
                self.get_string('mapping_header'),
                self.get_string('example_header'),
                self.get_string('notes_header')]

        for name in ('general','planning','tender','awards','contracts','implementation','parties'):
            sheets[name].append(headers)

        # add row to mapping sheet for each field in the schema
        for field in get_schema_fields(schema):

            # skip definitions section of schema, and deprecated fields
            if field.definition_pointer_components or field.deprecated:
                continue

            # set separator to use in field paths in output
            field.sep = '/'

            # set formatting keys for use in Google Sheets script
            if field.schema['type'] in ('object', 'array'):
                formatKey = 'span'
            else:
                formatKey = 'field'

            if field.required:
                formatPrefix = 'required_'
            else:
                formatPrefix = ''

            try:
                field_extension = self.field_extensions[field.path]
            except:
                field_extension = ''

            # add organization references to list for use in parties mapping sheet
            is_org_reference = (hasattr(field.schema, '__reference__') and field.schema.__reference__['$ref'] == '#/definitions/' + self.get_string('organization_reference_title')) \
                    or ('items' in field.schema and 'title' in field.schema['items'] and field.schema['items']['title'] == self.get_string('organization_reference_title'))

            if is_org_reference:
                row = [formatPrefix + formatKey, 1, field.path, field.schema['title'], field.schema['description']]

                if field_extension:
                    # if the org reference belongs to an extension, save it in a separate dict
                    # with the name of the extension
                    if field_extension not in org_refs_extensions.keys():
                        org_refs_extensions[field_extension] = []
                    org_refs_extensions[field_extension].append(row)
                else:
                    org_refs.append(row)

            # concatenate titles, descriptions and types for refs and arrays
            if  hasattr(field.schema, '__reference__'):
                title = field.schema.__reference__['title'] + ' (' + field.schema['title'] + ')'
                description = field.schema.__reference__['description'] + ' (' + field.schema['description'] + ')'

            elif 'items' in field.schema and 'properties' in field.schema['items'] and 'title' in field.schema['items']:
                title = field.schema['title'] + ' (' + field.schema['items']['title'] + ')'
                description  = field.schema['description'] + ' (' + field.schema['items'].get('description', '') + ')'

            else:
                title = field.schema['title']
                description = field.schema['description']

            # remove links from descriptions
            links = dict(INLINE_LINK_RE.findall(description))

            for key, link in links.items():
                description = description.replace('[' + key + '](' + link + ')', key)

            try:
                path = field.path[:field.path.index('/')]
            except:
                path = field.path

            if path in ('planning', 'tender', 'awards'):
                sheet = sheets[path]
                extension_key = path
            elif path == 'contracts':
                if 'contracts/implementation' in field.path:
                    sheet = sheets['implementation']
                    extension_key = 'implementation'
                else:
                    sheet = sheets['contracts']
                    extension_key = 'contracts'
            elif path == 'parties':
                sheet = parties_rows
                extension_key = 'parties'
            else:
                sheet = sheets['general']
                extension_key = 'general'

            row = [formatPrefix + formatKey, depth, field.path, title, description]

            field_is_stage = field.path in ('planning','tender','awards','contracts','contracts/implementation')

            if field_extension and not field_is_stage:
                # ignore the 'extension' field if it is set on one of the main sections

                if not field_extension in extension_rows[extension_key]:
                    extension_rows[extension_key][field_extension] = []
                extension_rows[extension_key][field_extension].append(row)
            else:
                # add row to mapping sheet
                sheet.append(row)

        # repeat fields from parties section for each organization reference 
        sheets['parties'].append(parties_rows[0]) # description of the parties section

        for ref in org_refs:
            sheets['parties'].append(ref)
            sheets['parties'].extend(parties_rows[1:])

        # add organizations from extensions

        # add extension section title
        if len(org_refs_extensions.keys()):
            sheets[name].append(['subtitle', 0, self.get_string('extension_section')])

        for extension_name,orgs in org_refs_extensions.items():
            # insert extension name
            text = extension_name + ': ' + self.extensions_info.get_description(extension_name)
            sheets['parties'].append(['subsubtitle',0,text])

            # insert organizations
            for org in orgs:
                sheets['parties'].append(org)
                sheets['parties'].extend(parties_rows[1:])

        for name in ('general','planning','tender','awards','contracts','implementation','parties'):

            if len(extension_rows[name]):
                # add extension section

                # add section title
                sheets[name].append(['subtitle', 0, self.get_string('extension_section')])

                for extension_name, rows in extension_rows[name].items():
                    text = extension_name + ': ' + self.extensions_info.get_description(extension_name)

                sheets[name].append(['subsubtitle',0,text])
                sheets[name].extend(rows)

            # add additional fields section to each sheet
            sheets[name].append(['subtitle', 0, self.get_string('additional_fields_note')])

            for i in range(4):
                sheets[name].append(['additional_field',0]) # was 1

            # make all rows have the same number of columns
            # (required for CSV parsing script in Google Sheets)
            for row in sheets[name]:
                if len(row) < len(headers):
                    for i in range(len(headers) - len(row)):
                        row.append('')

            return self._save_sheets(sheets)


    def _save_sheets(self, sheets):
        # save CSVs and upload to Google Drive
        drive = self.authenticate_pydrive()

        outputs = []
        for key, value in sheets.items():
            outputs.append({
                'sheet': value,
                'file': key + '_mapping.csv',
                'sheetname': self.get_string(key)
                })

            ids = []

        for output in outputs:
            with open(output['file'], 'w', encoding='utf8', newline='') as output_file:
                writer = csv.writer(output_file, dialect='excel')
                writer.writerows(output['sheet'])

            uploaded = drive.CreateFile({'title': output['sheetname']})
            uploaded.SetContentFile(output['file'])
            uploaded.Upload()
            ids.append(uploaded.get('id'))

        return ids

if __name__ == '__main__':
    g = MappingTemplateSheetsGenerator()
