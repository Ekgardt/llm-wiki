"""Global exact-record ownership; original files count only after full coverage."""
from tests.module_record_context import load_catalog, record_selections
from tests.module_records import _require, record_entries


def _catalog_identity(catalogs, name, catalog):
    source = catalog['source']
    previous = catalogs.get(source)
    _require(previous is None or previous[0] == name, 'Competing record catalogs')
    identifiers = {row['id'] for row in catalog['records']}
    catalogs[source] = (name, identifiers)


def _selection(root, selection, catalogs, tracked, context):
    name = selection['catalog']
    _require(name in tracked, 'Untracked record catalog')
    _require(name in context, 'Record catalog outside measured files')
    _path, catalog, source = load_catalog(root, name)
    _require(catalog['source'] in tracked, 'Untracked record source')
    _catalog_identity(catalogs, name, catalog)
    entries = record_entries(catalog, source, selection['ids'])
    return {(name, key) for key in entries}


def _selections(root, data, catalogs, tracked, context):
    result = set()
    for selection in record_selections(data):
        selected = _selection(root, selection, catalogs, tracked, context)
        _require(result.isdisjoint(selected), 'Duplicate declared record selection')
        result.update(selected)
    return result


def _register(owners, owner, claimed):
    for _catalog, entry in sorted(claimed):
        _require(entry not in owners, 'Duplicate record ownership: ' + entry)
        owners[entry] = owner


def _missing_records(catalogs, owners):
    missing = {}
    for source, (_name, identifiers) in sorted(catalogs.items()):
        absent = sorted(identifier for identifier in identifiers
                        if '@record/' + source + '#' + identifier not in owners)
        if absent:
            missing[source] = absent
    return missing


def _manifest_records(root, item, catalogs, tracked, owners):
    owner, data, context = item
    measured = _selections(root, data, catalogs, tracked, context)
    claimed = _selections(root, data['ownership'], catalogs, tracked, context)
    _require(claimed <= measured, 'Record ownership outside measured selection')
    _register(owners, owner, claimed)


def record_ownership_report(root, tracked, whole, contexts):
    catalogs, owners = {}, {}
    for item in contexts:
        _manifest_records(root, item, catalogs, tracked, owners)
    claimed_sources = {entry[len('@record/'):].rsplit('#', 1)[0] for entry in owners}
    _require(claimed_sources.isdisjoint(whole), 'Whole-file and record ownership overlap')
    partitioned = {source: value for source, value in catalogs.items() if source not in whole}
    missing = _missing_records(partitioned, owners)
    return {
        'record_ownership': dict(sorted(owners.items())),
        'partially_owned': missing,
        'fully_recorded': sorted(partitioned.keys() - missing.keys()),
    }
