import csv
import os
import re
import subprocess
from operator import itemgetter

import jsonref
import requests

try:
    from google.colab import auth
    from oauth2client.client import GoogleCredentials
except ImportError:
    # not running in a colab runtime, doesn't matter if running locally
    pass

from ocdsextensionregistry import ExtensionVersion, ProfileBuilder
from ocdsextensionregistry.util import replace_refs
from ocdskit.schema import get_schema_fields
from pydrive.auth import GoogleAuth
from pydrive.drive import GoogleDrive


class MappingTemplateSheetsGenerator:
    extension_field = "extension"

    def __init__(
        self,
        strings=None,
        schema_url=None,
        extension_urls=None,
        extension_descriptions=None,
        mapping_sheet="mapping-sheet.csv",
        lang="en",
        save_to="drive",
    ):
        self.strings = strings
        self.schema_url = schema_url
        self.mapping_sheet_file = mapping_sheet
        self.lang = lang
        self.extension_urls = extension_urls
        self.extension_descriptions = extension_descriptions
        self.field_extensions = {}
        self.save_to = save_to

        # read extension names per path from mapping-sheet
        with open(self.mapping_sheet_file) as f:
            reader = csv.DictReader(f, dialect="excel")
            row = next(reader)
            for row in reader:
                if row.get(self.extension_field):
                    self.field_extensions[row["path"]] = row[self.extension_field]

    def authenticate_pydrive(self):
        auth.authenticate_user()
        gauth = GoogleAuth()
        gauth.credentials = GoogleCredentials.get_application_default()
        return GoogleDrive(gauth)

    def get_string(self, key):
        return self.strings[key][self.lang]

    def get_patched_schema(self):
        schema_response = requests.get(self.schema_url, timeout=10)
        schema = schema_response.json()

        builder = ProfileBuilder(None, self.extension_urls)
        schema = builder.patched_release_schema(
            schema=schema, extension_field=self.extension_field, language=self.lang
        )
        schema = replace_refs(schema)
        with open("release-schema.json", "w") as f:
            jsonref.dump(schema, f)
        return schema

    def generate_mapping_sheets(self):
        # get schema
        schema = self.get_patched_schema()

        mapping_sheetnames = ("general", "planning", "tender", "awards", "contracts", "implementation")

        sheetnames = (*mapping_sheetnames, "schema", "schema_extensions")

        # create list for each mapping sheet
        sheets = {x: [] for x in sheetnames}
        sheet_headers = {x: [] for x in mapping_sheetnames}

        extension_rows = {
            x: {} for x in ("general", "planning", "tender", "awards", "contracts", "implementation", "parties")
        }

        # use the mapping sheet to load the schema and schema_extensions tabs
        header = []
        with open(self.mapping_sheet_file) as csvfile:
            readme = csv.reader(csvfile, dialect="excel")
            header = next(readme)

            sheets["schema"].append(header[:-1])

            for row in readme:
                url = row[7]
                url = url.replace("1.1-dev", "1.1.5")
                row[7] = url
                if row[10]:
                    sheets["schema_extensions"].append(row)
                else:
                    sheets["schema"].append(row[:-1])

        # move the extension column to the beginning
        sheets["schema_extensions"] = [row[-1:] + row[1:-1] for row in sheets["schema_extensions"]]

        # sort the Extension Schemas by extension, stage and path
        sheets["schema_extensions"].sort(key=itemgetter(0, 1))

        # add header
        sheets["schema_extensions"] = [header[-1:] + header[1:-1]] + sheets["schema_extensions"]

        # create list for fields to repeat on parties sheet
        parties_rows = []

        # create list for organization references to add to parties sheet
        org_refs = []
        org_refs_extensions = {}

        # set default depth for row grouping in Google Sheets
        depth = 0

        # regular expression to find links in schema descriptions
        inline_link_re = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

        # remove links from top-level schema description
        for key, link in inline_link_re.findall(schema["description"]):
            schema["description"] = schema["description"].replace("[" + key + "](" + link + ")", key)

        # add header rows to each sheet
        headers = [
            "column_headers",
            depth,
            self.get_string("path_header"),
            self.get_string("title_header"),
            self.get_string("description_header"),
            self.get_string("mapping_header"),
            self.get_string("example_header"),
            self.get_string("notes_header"),
        ]

        # add row to mapping sheet for each field in the schema
        for field in get_schema_fields(schema):
            if field.deprecated:
                continue

            # set separator to use in field paths in output
            field.sep = "/"

            # is this field from an extension?
            field_extension = self.field_extensions.get(field.path, "")

            # is this field a top-level stage?
            field_is_stage = field.path in {"planning", "tender", "awards", "contracts", "contracts/implementation"}

            # set formatting keys for use in Google Sheets script
            if field_is_stage:
                format_key = "title"
            elif field.schema["type"] in ("object", "array"):
                format_key = "span"
            else:
                format_key = "field"

            if field_extension:
                format_prefix = "extension_"
            elif field.required:
                format_prefix = "required_"
            else:
                format_prefix = ""

            # add organization references to list for use in parties mapping sheet
            title = self.get_string("organization_reference_id_title")
            if (
                field.schema.get("properties", {}).get("id", {}).get("title") == title
                or field.schema.get("items", {}).get("properties", {}).get("id", {}).get("title") == title
            ):
                row = [format_prefix + format_key, 1, field.path]

                if field_extension:
                    # if the org reference belongs to an extension, save it in a separate dict
                    # with the name of the extension
                    if field_extension not in org_refs_extensions:
                        org_refs_extensions[field_extension] = []
                    org_refs_extensions[field_extension].append(row)
                else:
                    org_refs.append(row)

            try:
                path = field.path[: field.path.index("/")]
            except ValueError:
                path = field.path

            if path in {"planning", "tender", "awards"}:
                sheet = sheets[path]
                sheetname = path
            elif path == "contracts":
                if "contracts/implementation" in field.path:
                    sheet = sheets["implementation"]
                    sheetname = "implementation"
                else:
                    sheet = sheets["contracts"]
                    sheetname = "contracts"
            elif path == "parties":
                sheet = parties_rows
                sheetname = "parties"
            else:
                sheet = sheets["general"]
                sheetname = "general"

            if format_key == "title":
                sheet_headers[sheetname].append(
                    [
                        format_key,
                        depth,
                        f"{self.get_string('standard_name')}: {field.schema['title']}",
                    ]
                )
                sheet_headers[sheetname].append(
                    [
                        "subtitle",
                        depth,
                        field.schema["description"],
                    ]
                )
                continue

            row = [format_prefix + format_key, depth, field.path]

            if field_extension:
                if field_extension not in extension_rows[sheetname]:
                    extension_rows[sheetname][field_extension] = []
                extension_rows[sheetname][field_extension].append(row)
            else:
                # add row to mapping sheet
                sheet.append(row)

        # add a static header for the General sheet

        sheet_headers["general"].append(
            ["title", depth, f"{self.get_string('standard_name')}: {self.get_string('general_title')}"]
        )
        sheet_headers["general"].append(
            [
                "subtitle",
                depth,
                self.get_string("general_help_text"),
            ]
        )

        # add headers for each sheet
        for name in mapping_sheetnames:
            sheets[name] = sheet_headers[name] + [headers] + sheets[name]

        # repeat fields from parties section for each organization reference
        sheets["general"].append(
            ["subtitle", depth, self.get_string("parties_description")]
        )  # description of the parties section

        for ref in org_refs:
            ref[0] = "ref_span"
            sheets["general"].append(ref)
            sheets["general"].extend(parties_rows[1:])

        # add organizations from extensions

        extension_parties_rows = [["extension_" + x[0], x[1], x[2]] for x in parties_rows[1:]]

        for extension_name, orgs in org_refs_extensions.items():
            # insert extension name
            if extension_name not in extension_rows["general"]:
                extension_rows["general"][extension_name] = []

            # insert organizations
            for org in orgs:
                extension_rows["general"][extension_name].append(org)
                extension_rows["general"][extension_name].extend(extension_parties_rows)

        for name in mapping_sheetnames:
            if len(extension_rows[name]):
                # add extension section

                # add section title
                sheets[name].append(["section", 0, self.get_string("extension_section")])

                for extension_name, rows in extension_rows[name].items():
                    text = extension_name + ": " + self.extension_descriptions[extension_name]

                    sheets[name].append(["extension", 0, text])
                    sheets[name].extend(rows)

            # add additional fields section to each sheet
            sheets[name].append(["section", 0, self.get_string("additional_fields_note")])

            for _ in range(4):
                sheets[name].append(["additional_field", 0])  # was 1

            # make all rows have the same number of columns
            # (required for CSV parsing script in Google Sheets)
            for row in sheets[name]:
                if len(row) < len(headers):
                    for _ in range(len(headers) - len(row)):
                        row.append("")

        return self._save_sheets(sheets)

    def _save_sheets(self, sheets):
        if self.save_to == "drive":
            # save CSVs and upload to Google Drive
            drive = self.authenticate_pydrive()

        outputs = []
        for key, value in sheets.items():
            outputs.append(
                {
                    "sheet": value,
                    "file": os.path.join("output", key + "_mapping.csv"),
                    "sheetname": self.get_string(key + "_sheetname"),
                }
            )

        ids = []

        os.makedirs("output", exist_ok=True)
        for output in outputs:
            with open(output["file"], "w", encoding="utf8", newline="") as output_file:
                writer = csv.writer(output_file, dialect="excel")
                writer.writerows(output["sheet"])

            if self.save_to == "drive":
                uploaded = drive.CreateFile({"title": output["sheetname"]})
                uploaded.SetContentFile(output["file"])
                uploaded.Upload()
                ids.append(uploaded.get("id"))

        return ids


if __name__ == "__main__":
    strings = {
        "path_header": {
            "en": "Path",
            "es": "Rutas",
        },
        "type_header": {
            "en": "Type",
            "es": "Tipo",
        },
        "title_header": {
            "en": "Title",
            "es": "Título",
        },
        "description_header": {
            "en": "Description",
            "es": "Descripción",
        },
        "mapping_header": {
            "en": "Mapping",
            "es": "Mapear",
        },
        "example_header": {
            "en": "Example",
            "es": "Ejemplo",
        },
        "notes_header": {
            "en": "Notes",
            "es": "Notas",
        },
        "general_help_text": {
            "en": "Fields in this section apply at release level. Each release provides data about a single contracting process at a particular point in time. Releases can be used to notify users of new tenders, awards, contracts, and other updates",  # noqa: E501
            "es": "Los campos de esta sección aplican a nivel de entrega. Cada entrega provee datos sobre un proceso de contratación único en un momento particular en el tiempo. Las entregas pueden ser usadas para notificar a los usuarios de nuevas licitaciones, adjudicaciones y otras actualizaciones.",  # noqa: E501
        },
        "additional_fields_note": {
            "en": "If you have additional information applicable at this level and not covered by the core OCDS schema or extensions, list the data items below, along with a proposed description. This information can be used to develop new OCDS extensions.",  # noqa: E501
            "es": "Si tiene información adicional que aplique a este nivel y que no está cubierto por el esquema OCDS principal o extensiones, agregue los elementos de datos a continuación, junto con una descripción propuesta. Esta información podrá ser utilizada para crear nuevas extensiones OCDS.",  # noqa: E501
        },
        "extension_section": {
            "en": "Extensions are additions to the core OCDS schema which allow publishers to include extra information in their OCDS data. The following extensions are available for the present section:",  # noqa: E501
            "es": "Las extensiones son adiciones al esquema OCDS principal que permiten que los publicadores incluyan información extra en sus datos OCDS. Las siguientes extensiones están disponibles para la presente sección:",  # noqa: E501
        },
        "parties_description": {
            "en": "Parties: Information on the parties (organizations, economic operators and other participants) who are involved in the contracting process and their roles, e.g. buyer, procuring entity, supplier etc. Organization references elsewhere in the schema are used to refer back to this entries in this list.",  # noqa: E501
            "es": "Partes: Información sobre las partes (organizaciones, operadores económicos y otros participantes) que están involucrados en el proceso de contratación y sus roles, ej. comprador, entidad contratante, proveedor, etc. Las referencias a organizaciones en otros lugares del esquema son usados para referirse de vuelta a estas entradas en la lista.",  # noqa: E501
        },
        "standard_name": {
            "en": "Open Contracting Data Standard",
            "es": "Estándar de Datos de Contrataciones Abiertas",
        },
        "organization_reference_id_title": {
            "en": "Organization ID",
            "es": "ID de Organización",
        },
        "overview": {
            "en": "Field Level Mapping Overview",
            "es": "Descripción Mapeo a Nivel de Campos",
        },
        "source_systems": {
            "en": "(Source) 1. Systems",
            "es": "(Fuentes) 1. Sistemas",
        },
        "source_fields": {
            "en": "(Source) 2. Fields",
            "es": "(Fuentes) 1. Campos",
        },
        "general_sheetname": {
            "en": "(OCDS) 1. General (all stages)",
            "es": "(OCDS) 1. General (todas las etapas)",
        },
        "general_title": {
            "en": "General (all stages)",
            "es": "General (todas las etapas)",
        },
        "planning_sheetname": {
            "en": "(OCDS) 2. Planning",
            "es": "(OCDS) 2. Planificación",
        },
        "tender_sheetname": {
            "en": "(OCDS) 3. Tender",
            "es": "(OCDS) 3. Licitación",
        },
        "awards_sheetname": {
            "en": "(OCDS) 4. Award",
            "es": "(OCDS) 4. Adjudicación",
        },
        "contracts_sheetname": {
            "en": "(OCDS) 5. Contract",
            "es": "(OCDS) 5. Contrato",
        },
        "implementation_sheetname": {
            "en": "(OCDS) 6. Implementation",
            "es": "(OCDS) 6. Implementación",
        },
        "schema_sheetname": {
            "en": "OCDS Schema 1.1.5",
            "es": "Esquema OCDS 1.1.5",
        },
        "schema_extensions_sheetname": {
            "en": "OCDS Extension Schemas 1.1.5",
            "es": "Esquemas de Extensiones OCDS 1.1.5",
        },
    }

    lang = "en"
    schema_url = "https://standard.open-contracting.org/1.1/en/release-schema.json"

    extension_urls = [
        f"https://extensions.open-contracting.org/{lang}/extensions/submissionTerms/master/",
        f"https://extensions.open-contracting.org/{lang}/extensions/bids/master/",
        f"https://extensions.open-contracting.org/{lang}/extensions/enquiries/master/",
        f"https://extensions.open-contracting.org/{lang}/extensions/location/master/",
        f"https://extensions.open-contracting.org/{lang}/extensions/lots/master/",
        f"https://extensions.open-contracting.org/{lang}/extensions/participation_fee/master/",
        f"https://extensions.open-contracting.org/{lang}/extensions/process_title/master/",
    ]

    with open("release-schema.json", "w") as f:
        f.write(requests.get(schema_url, timeout=10).text)

    with open("mapping-sheet.csv", "w") as f:
        subprocess.run(  # noqa: S603 # trusted input
            [  # noqa: S607
                "ocdskit",
                "mapping-sheet",
                "release-schema.json",
                "--extension",
                *extension_urls,
                "--extension-field",
                "extension",
                "--language",
                lang,
            ],
            check=True,
            stdout=f,
        )

    extension_descriptions = {}
    for extension_url in extension_urls:
        data = dict.fromkeys(["Id", "Date", "Version", "Base URL", "Download URL"])
        data["Base URL"] = extension_url
        version = ExtensionVersion(data)
        extension_descriptions[version.metadata["name"][lang]] = version.metadata["description"][lang]

    g = MappingTemplateSheetsGenerator(
        lang=lang,
        schema_url=schema_url,
        extension_urls=extension_urls,
        extension_descriptions=extension_descriptions,
        strings=strings,
        save_to="local",
    )
    g.generate_mapping_sheets()
