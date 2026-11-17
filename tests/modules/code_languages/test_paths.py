"""Pure final-suffix semantics do not depend on host filesystem access."""
from pathlib import PurePosixPath, PureWindowsPath

import pytest
from code_languages import CODE_LANGUAGE_BY_SUFFIX, language_for_path


@pytest.mark.parametrize('flavor', [PurePosixPath, PureWindowsPath])
@pytest.mark.parametrize(('name', 'expected'), [
    ('a.PY', 'python'), ('module.generated.D.TS', 'typescript'),
    ('a.h', 'c'), ('a.hpp', 'cpp'), ('a.tsx', 'typescript'),
    ('file.py.txt', None), ('README', None), ('.py', None), ('file.', None),
])
def test_final_suffix_without_file_access(flavor, name, expected):
    assert language_for_path(flavor(name)) == expected


def test_suffix_policy_cannot_be_mutated():
    with pytest.raises(TypeError):
        CODE_LANGUAGE_BY_SUFFIX['.unsupported'] = 'python'
    assert '.unsupported' not in CODE_LANGUAGE_BY_SUFFIX
