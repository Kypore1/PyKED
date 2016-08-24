"""Validation functions for values.

Based on validation module of pyrk (https://github.com/pyrk).

.. moduleauthor:: Kyle Niemeyer <kyle.niemeyer@gmail.com>
"""

# Python 2 compatibility
from __future__ import print_function
from __future__ import division

import sys
from warnings import warn
import re

import pkg_resources
try:
    import ruamel.yaml as yaml
except ImportError:
    import yaml

import pint
from requests.exceptions import HTTPError, ConnectionError
from cerberus import Validator
import habanero
from orcid import SearchAPI

# Local imports
from .utils import units

if sys.version_info > (3,):
    long = int
    from functools import reduce

orcid_api = SearchAPI(sandbox=False)

# Load the ChemKED schema definition file
schema_file = pkg_resources.resource_filename(__name__, 'chemked_schema.yaml')
with open(schema_file, 'r') as f:
    schema = yaml.safe_load(f)

# These top-level keys in the schema server as references for lower-level keys.
# They are removed to prevent conflicts due to required variables, etc.
for key in ['author', 'value-unit-required', 'value-unit-optional', 'composition', 'ignition-type']:
    schema.pop(key)

# SI units for available value-type properties
property_units = {'temperature': 'kelvin',
                  'pressure': 'pascal',
                  'ignition-delay': 'second',
                  'pressure-rise': '1.0 / second',
                  'compression-time': 'second',
                  'volume': 'meter**3',
                  'time': 'second',
                  }


def compare_name(given_name, family_name, question_name):
    """Compares a name in question to a specified name separated into given and family.

    The name in question ``question_name`` can be of varying format, including
    "Kyle E. Niemeyer", "Kyle Niemeyer", "K. E. Niemeyer", "KE Niemeyer", and
    "K Niemeyer". Other possibilities include names with hyphens such as
    "Chih-Jen Sung", "C. J. Sung", "C-J Sung".
    """
    # lowercase everything
    given_name = given_name.lower()
    family_name = family_name.lower()
    question_name = question_name.lower()

    # rearrange names given as "last, first middle"
    if ',' in question_name:
        name_split = question_name.split(',')
        name_split.reverse()
        question_name = ' '.join(name_split).strip()

    # split name in question by , <space> - .
    name_split = list(filter(None, re.split("[, \-.]+", question_name)))
    first_name = [name_split[0]]
    if len(name_split) == 3:
        first_name += [name_split[1]]

    given_name = list(filter(None, re.split("[, \-.]+", given_name)))

    if len(first_name) == 2 and len(given_name) == 2:
        # both have first and middle name/initial
        first_name[1] = first_name[1][0]
        given_name[1] = given_name[1][0]
    elif len(given_name) == 2 and len(first_name) == 1:
        del given_name[1]
    elif len(first_name) == 2 and len(given_name) == 1:
        del first_name[1]

    # first initial
    if len(first_name[0]) == 1 or len(given_name[0]) == 1:
        given_name[0] = given_name[0][0]
        first_name[0] = first_name[0][0]

    # first and middle initials combined
    if len(first_name[0]) == 2 or len(given_name[0]) == 2:
        given_name[0] = given_name[0][0]
        first_name[0] = name_split[0][0]

    return given_name == first_name and family_name == name_split[-1]


class OurValidator(Validator):
    """Custom validator with rules for units and Quantities.
    """
    def _validate_isvalid_unit(self, isvalid_unit, field, value):
        """Checks for appropriate units.
        """
        if isvalid_unit:
            quantity = 1.0 * units(value['units'])
            try:
                quantity.to(property_units[field])
            except pint.DimensionalityError:
                self._error(field, 'incompatible units; should be consistent '
                            'with ' + property_units[field]
                            )

    def _validate_isvalid_quantity(self, isvalid_quantity, field, value):
        """Checks for valid given value and appropriate units.
        """
        if isvalid_quantity:
            quantity = value['value'] * units(value['units'])
            low_lim = 0.0 * units(property_units[field])

            try:
                if quantity <= low_lim:
                    self._error(field, 'value must be > 0.0')
            except pint.DimensionalityError:
                self._error(field, 'incompatible units; should be consistent '
                            'with ' + property_units[field]
                            )

    def _validate_isvalid_reference(self, isvalid_reference, field, value):
        """Checks valid reference metadata using DOI (if present).

        Todo:
            * remove UnboundLocalError from exception handling
        """
        if isvalid_reference and 'doi' in value:
            try:
                ref = habanero.Crossref().works(ids=value['doi'])['message']
            except (HTTPError, habanero.RequestError):
                self._error(field, 'DOI not found')
                return
            # TODO: remove UnboundLocalError after habanero fixed
            except (ConnectionError, UnboundLocalError):
                warn('network not available, DOI not validated.')
                return

            # check journal name
            if value.get('journal') and value['journal'] not in ref['container-title']:
                self._error(field, 'journal does not match: ' +
                            ', '.join(ref['container-title'])
                            )
            # check year
            pub_year = (ref.get('published-print')
                        if ref.get('published-print')
                        else ref.get('published-online')
                        )['date-parts'][0][0]

            if value.get('year') and value['year'] != pub_year:
                self._error(field, 'year should be ' + str(pub_year))

            # check volume number
            if value.get('volume') and value['volume'] != int(ref['volume']):
                self._error(field, 'volume number should be ' + ref['volume'])

            # check pages
            if value.get('pages') and value['pages'] != ref['page']:
                self._error(field, 'pages should be ' + ref['page'])

            # check that all authors present
            author_list = value['authors'][:]
            for author in ref['author']:
                # find using family name
                author_match = next(
                    (a for a in author_list if
                     a['name'].split()[-1].upper() == author['family'].upper()),
                    None
                    )
                if author_match is None:
                    self._error(field, 'missing author ' +
                                ' '.join([author['given'], author['family']])
                                )
                else:
                    # validate ORCID if given
                    orcid = author.get('ORCID')
                    if orcid:
                        # Crossref may give ORCID as http://orcid.org/####-####-####-####
                        # so need to strip the leading URL
                        orcid = orcid[orcid.rfind('/') + 1:]

                        if 'ORCID' in author_match:
                            if author_match['ORCID'] != orcid:
                                self._error(
                                    field, author_match['name'] + ' ORCID does ' +
                                    'not match that in reference. Reference: ' +
                                    orcid + '. Given: ' + author_match['ORCID']
                                    )
                        else:
                            # ORCID not given, suggest adding it
                            warn('ORCID ' + orcid + ' missing for ' + author_match['name'])

    def _validate_isvalid_orcid(self, isvalid_orcid, field, value):
        """Checks for valid ORCID if given.
        """
        if isvalid_orcid and 'ORCID' in value:
            try:
                res = orcid_api.search_public('orcid:' + value['ORCID'])
            except ConnectionError:
                warn('network not available, ORCID not validated.')
                return

            # Return error if no results are found for the given ORCID
            if res['orcid-search-results']['num-found'] == 0:
                self._error(field, 'ORCID incorrect or invalid for ' +
                            value['name']
                            )
                return

            maplist = ['orcid-search-results', 'orcid-search-result', 0,
                       'orcid-profile', 'orcid-bio', 'personal-details',
                       'family-name', 'value'
                       ]
            family_name = reduce(lambda d, k: d[k], maplist, res)
            maplist[-2] = 'given-names'
            given_name = reduce(lambda d, k: d[k], maplist, res)

            if not compare_name(given_name, family_name, value['name']):
                self._error(field, 'Name and ORCID do not match. Name supplied: ' +
                            value['name'] + '. Name associated with ORCID: ' +
                            ' '.join([given_name, family_name])
                            )
