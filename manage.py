import csv
import gettext
import logging
import os
import re
import subprocess
from operator import itemgetter
from pathlib import Path

import click
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

logger = logging.getLogger(__name__)
localedir = Path(__file__).absolute().parent / "locale"


class MappingTemplateSheetsGenerator:
    extension_field = "extension"

    def __init__(
        self,
        schema_url=None,
        extension_urls=None,
        extension_descriptions=None,
        mapping_sheet="mapping-sheet.csv",
        lang="en",
        save_to="drive",
    ):
        self.schema_url = schema_url
        self.mapping_sheet_file = mapping_sheet
        self.lang = lang
        self.extension_urls = extension_urls
        self.extension_descriptions = extension_descriptions
        self.field_extensions = {}
        self.save_to = save_to
        self._ = gettext.translation("messages", localedir, languages=[lang], fallback=lang == "en").gettext

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
            self._("Path"),
            self._("Title"),
            self._("Description"),
            self._("Mapping"),
            self._("Example"),
            self._("Notes"),
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
            title = self._("Organization ID")
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
                        "{}: {}".format(self._("Open Contracting Data Standard"), field.schema["title"]),
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
            ["title", depth, "{}: {}".format(self._("Open Contracting Data Standard"), self._("General (all stages)"))]
        )
        sheet_headers["general"].append(
            [
                "subtitle",
                depth,
                self._(
                    "Fields in this section apply at release level. Each release provides data about a "
                    "single contracting process at a particular point in time. Releases can be used to "
                    "notify users of new tenders, awards, contracts, and other updates"
                ),
            ]
        )

        # add headers for each sheet
        for name in mapping_sheetnames:
            sheets[name] = sheet_headers[name] + [headers] + sheets[name]

        # repeat fields from parties section for each organization reference
        sheets["general"].append(
            [
                "subtitle",
                depth,
                self._(
                    "Parties: Information on the parties (organizations, economic operators and other "
                    "participants) who are involved in the contracting process and their roles, e.g. buyer, "
                    "procuring entity, supplier etc. Organization references elsewhere in the schema are used "
                    "to refer back to this entries in this list."
                ),
            ]
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
                sheets[name].append(
                    [
                        "section",
                        0,
                        self._(
                            "Extensions are additions to the core OCDS schema which allow publishers to "
                            "include extra information in their OCDS data. The following extensions are "
                            "available for the present section:"
                        ),
                    ]
                )

                for extension_name, rows in extension_rows[name].items():
                    text = extension_name + ": " + self.extension_descriptions[extension_name]

                    sheets[name].append(["extension", 0, text])
                    sheets[name].extend(rows)

            # add additional fields section to each sheet
            sheets[name].append(
                [
                    "section",
                    0,
                    self._(
                        "If you have additional information applicable at this level and not covered by the "
                        "core OCDS schema or extensions, list the data items below, along with a proposed "
                        "description. This information can be used to develop new OCDS extensions."
                    ),
                ]
            )

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

        sheetname_map = {
            "general": self._("(OCDS) 1. General (all stages)"),
            "planning": self._("(OCDS) 2. Planning"),
            "tender": self._("(OCDS) 3. Tender"),
            "awards": self._("(OCDS) 4. Award"),
            "contracts": self._("(OCDS) 5. Contract"),
            "implementation": self._("(OCDS) 6. Implementation"),
            "schema": self._("OCDS Schema 1.1.5"),
            "schema_extensions": self._("OCDS Extension Schemas 1.1.5"),
        }

        outputs = []
        for key, value in sheets.items():
            outputs.append(
                {
                    "sheet": value,
                    "file": os.path.join("output", key + "_mapping.csv"),
                    "sheetname": sheetname_map.get(key, key),
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


@click.command()
@click.option(
    "-l",
    "--lang",
    type=click.Choice(["en", "es"]),
    default="en",
    show_default=True,
    help="Schema language",
)
@click.option(
    "-s",
    "--schema-url",
    metavar="URL",
    help="OCDS release schema URL (default: https://standard.open-contracting.org/1.1/[lang]/release-schema.json)",
)
@click.option(
    "-e",
    "--extension",
    "extension_urls",
    multiple=True,
    metavar="URL",
    help="Extension URL like https://extensions.open-contracting.org/en/extensions/lots/master/ or identifier like "
    "lots/master (can be specified multiple times)",
)
@click.option(
    "--recommended",
    is_flag=True,
    help="Include all recommended extensions (bids, enquiries, location, lots, participation_fee, process_title)",
)
@click.option(
    "--save-to",
    type=click.Choice(["local", "drive"]),
    default="local",
    show_default=True,
    help="Where to save output files ('drive' requires authentication for Google Drive)",
)
def main(lang, schema_url, extension_urls, recommended, save_to):
    """Generate OCDS field-level mapping template sheets."""
    if not schema_url:
        schema_url = f"https://standard.open-contracting.org/1.1/{lang}/release-schema.json"

    if recommended:
        extension_urls.extend(
            f"{e}/v1.1.5" for e in ("bids", "enquiries", "location", "lots", "participation_fee", "process_title")
        )

    extension_urls = [
        extension_url
        if extension_url.startswith(("http://", "https://"))
        else f"https://extensions.open-contracting.org/{lang}/extensions/{extension_url}/"
        for extension_url in extension_urls
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
        save_to=save_to,
    )
    g.generate_mapping_sheets()


if __name__ == "__main__":
    main()
