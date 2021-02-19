import json
import requests
import shutil
from glob import glob
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from ocds_babel.translate import translate
from ocdsextensionregistry import ExtensionRegistry


class ExtensionsInfo(object):
        
    extensions_url = 'https://raw.githubusercontent.com/open-contracting/extension_registry/main/extensions.csv'
    extension_versions_url = 'https://raw.githubusercontent.com/open-contracting/extension_registry/main/extension_versions.csv'
    

    def __init__(self, lang='en', exclusions=['milestone_documents'], version='master'):
        self.lang = lang
        self.exclusions = exclusions
        self.version = version
        self.descriptions = {}
        

    def get_description(self, extension_name):
        """
        Gets a description for a extension, given its (translated) name
        """
        return self.descriptions[extension_name]


    def load_extensions_info(self):
        """
        Gets the core extensions from the extension registry.

        If the language is not 'en', produces the translated versions for each core extension.
        """
        extensions_dir = Path('extensions')
        if extensions_dir.exists():
            shutil.rmtree(str(extensions_dir))
        extensions_dir.mkdir()

        # download extension files according to version
        registry = ExtensionRegistry(self.extension_versions_url, self.extensions_url)
        for version in registry.filter(core=True, version=self.version):
            if version.id not in self.exclusions:
                zip_file = version.zipfile()
                zip_file.extractall(path=str(extensions_dir))
                # rename path to extension id
                path = extensions_dir / zip_file.infolist()[0].filename
                path.rename(extensions_dir / version.id)

        if self.lang is 'en':
            
            output_dir = extensions_dir

        else:
            # translate core extensions

            translation_sources_dir = Path('ocds-extensions-translations-master')
            if translation_sources_dir.exists():
                shutil.rmtree(str(translation_sources_dir))
            
            res = requests.get('https://github.com/open-contracting/ocds-extensions-translations/archive/master.zip')
            res.raise_for_status()
            content = BytesIO(res.content)
            with ZipFile(content) as zipfile:
                zipfile.extractall()

            output_dir = Path('translations') / self.lang
            if output_dir.exists():
                shutil.rmtree(str(output_dir))
            output_dir.mkdir(parents=True)
            locale = str(translation_sources_dir / 'locale')
            headers = ['Title', 'Description', 'Extension']

            for dir in [x for x in extensions_dir.iterdir() if x.is_dir()]:
                translate([
                    (glob(str(dir / 'extension.json')), output_dir / dir.parts[-1], dir.parts[-1] + '/' + self.version + '/schema'),
                    (glob(str(dir / 'release-schema.json')), 
                        output_dir / dir.parts[-1], 
                        dir.parts[-1] + '/' + self.version + '/schema')
                    ], locale, self.lang, headers)
        
        self.extension_urls = [path.resolve(strict=True).as_uri() for path in output_dir.iterdir() if path.is_dir()]
        
        # get names and descriptions for each extension
        for dir in [x for x in output_dir.iterdir() if x.is_dir()]:
            path = dir.joinpath('extension.json')
            info = {}
            with path.open() as f:
                info = json.load(f)
                self.descriptions[info['name'][self.lang]] = info['description'][self.lang]
                # mapping-schema looks for the name of the name extension in English
                if self.lang is not 'en':
                    info['name']['en'] = info['name'][self.lang]
            with path.open(mode='w') as f:
                json.dump(info, f)
        
        return self.extension_urls


if __name__ == '__main__':
    extensions = ExtensionsInfo(lang='es', version='v1.1.5')

    urls = extensions.load_extensions_info()

    print(urls)
