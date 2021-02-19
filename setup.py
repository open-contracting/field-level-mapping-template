from setuptools import find_packages, setup

setup(
    name='ocds-mappingtemplate-generator',
    version='0.0.1',
    author='Open Contracting Partnership',
    author_email='data@open-contracting.org',
    description='Utilities to generate the OCDS Field-Level Mapping Template',
    packages=find_packages(),
    install_requires=[
        'commonmark @ git+https://github.com/readthedocs/commonmark.py.git@dafae75015cc342f3fddb499674bab97ac4a6a96#egg=commonmark',  # noqa: E501
        'ocds-babel[markdown]>=0.2.2',
        'ocdsextensionregistry>=0.0.22',
        'ocdskit>=0.2.11',
        'PyDrive>=1.3.1',
        'Sphinx==2.2.1'
    ]
)
