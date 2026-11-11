"""Model roles preserve text and the pinned encoder contract."""
import embedding_model
import pytest


@pytest.mark.parametrize(('is_query', 'prefix'), [(True, 'query:'), (False, 'passage:')])
def test_roles_prefix_every_text_without_stripping_or_mutating(is_query, prefix):
    texts = ['', '  文 Я 😀', 'query: already present']
    before = list(texts)
    assert embedding_model.prefixed_texts(texts, is_query) == [
        prefix + ' ', prefix + '   文 Я 😀', prefix + ' query: already present',
    ]
    assert texts == before
    assert embedding_model.embedding_instruction(is_query) == prefix


def test_empty_input_and_disabled_instruction_return_independent_lists(monkeypatch):
    empty = []
    assert embedding_model.prefixed_texts(empty, True) == []
    assert embedding_model.prefixed_texts(empty, True) is not empty
    monkeypatch.setattr(embedding_model, 'QUERY_INSTRUCTION', '')
    texts = ['unchanged']
    result = embedding_model.prefixed_texts(texts, True)
    assert result == texts and result is not texts


def test_pinned_identity_matches_the_model_card_and_verified_artifact_contract():
    assert embedding_model.EMBEDDING_MODEL == 'intfloat/multilingual-e5-small'
    assert embedding_model.EMBEDDING_MODEL_REVISION == '614241f622f53c4eeff9890bdc4f31cfecc418b3'
    assert embedding_model.EMBEDDING_DIM == 384
    assert embedding_model.EMBEDDING_READ_FILES == ('onnx/model.onnx', 'tokenizer.json')
    assert embedding_model.EMBEDDING_RETIRED_FILES == ('model.safetensors',)
