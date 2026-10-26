import importlib.util
import inspect
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from scripts.datalab_analysis import AnalysisInvariantError, RowPartition, aligned_groups, StageCache


def test_row_partition_docs_distinguish_in_place_mutation_from_evidence():
    exclude_doc = inspect.getdoc(RowPartition.exclude)
    verify_doc = inspect.getdoc(RowPartition.verify)
    assert 'Returns\n-------\nNone\n' in exclude_doc
    assert 'in place' in exclude_doc and 'separate statement' in exclude_doc
    assert 'Returns\n-------\ndict\n' in verify_doc
    assert 'evidence dict, not a list' in verify_doc
    assert 'evidence = audit.verify(retained_ids)' in verify_doc


def test_documented_partition_usage_retains_ids_and_existing_return_behavior():
    audit = RowPartition([10, 20, 30, 40])
    returned = audit.exclude('missing', [20])
    retained_ids = [10, 30, 40]
    evidence = audit.verify(retained_ids)
    assert returned is None
    assert retained_ids == [10, 30, 40]
    assert evidence == {'passed': True, 'original_rows': 4, 'accepted_rows': 3,
                        'excluded_rows': {'missing': 1}}


def test_documented_grouping_uses_named_row_identity_or_explicit_positional_arrays():
    doc = inspect.getdoc(aligned_groups)
    assert 'set_index' in doc and 'to_numpy' in doc
    assert 'dictionary mapping each group label to a NumPy ndarray' in doc
    retained = pd.DataFrame({'row_id': [10, 30, 40], 'value': [1., 2., 3.],
                             'group': ['A', 'B', 'A']})
    indexed = retained.set_index('row_id')
    for values, labels in ((indexed.value, indexed.group),
                           (retained.value.to_numpy(), retained.group.to_numpy())):
        groups = aligned_groups(values, row_ids=indexed.index, labels=labels)
        assert isinstance(groups, dict)
        assert all(isinstance(group, np.ndarray) for group in groups.values())
        assert groups['A'].tolist() == [1., 3.] and groups['B'].tolist() == [2.]
    with pytest.raises(AnalysisInvariantError, match='index'):
        aligned_groups(retained.value, row_ids=indexed.index, labels=retained.group)


def test_stage_cache_documented_result_stays_an_object(tmp_path):
    assert 'Returns\n-------\nStageResult\n' in inspect.getdoc(StageCache.run)
    result = stage(StageCache(tmp_path / 'stages'))
    assert not isinstance(result, dict)
    assert result.reused is False and isinstance(result.evidence, dict)
    assert isinstance(result.outputs['result'], Path)


def test_partition_preserves_original_rows_across_all_exclusion_steps():
    audit = RowPartition(range(8))
    audit.exclude('qc', [1])
    audit.exclude('missing', [3, 4])
    audit.exclude('nonpositive', [6])
    assert audit.verify([0, 2, 5, 7]) == {
        'passed': True, 'original_rows': 8, 'accepted_rows': 4,
        'excluded_rows': {'qc': 1, 'missing': 2, 'nonpositive': 1}}
    with pytest.raises(AnalysisInvariantError, match='Original rows'):
        audit.verify([0, 2, 5])


@pytest.mark.parametrize('original', [[1, 1], [None], [float('nan')], [pd.NA], [('A', float('nan'))]])
def test_original_row_identity_is_explicit_and_unique(original):
    with pytest.raises(AnalysisInvariantError):
        RowPartition(original)


def test_overlap_and_foreign_rows_do_not_mutate_partition():
    audit = RowPartition([1, 2, 3])
    audit.exclude('missing', [2])
    for reason, ids in [('qc', [2]), ('other', [4]), ('missing', [1])]:
        with pytest.raises(AnalysisInvariantError):
            audit.exclude(reason, ids)
    assert audit.verify([1, 3])['accepted_rows'] == 2


def test_filtered_middle_row_does_not_lose_or_reassign_residuals():
    ids = [0, 1, 3, 4]
    labels = pd.Series(['A', 'A', 'B', 'B'], index=ids)
    groups = aligned_groups(np.array([10, 20, 30, 40]), row_ids=ids, labels=labels)
    assert groups['A'].tolist() == [10, 20]
    assert groups['B'].tolist() == [30, 40]
    assert sum(map(len, groups.values())) == 4
    with pytest.raises(AnalysisInvariantError, match='values index'):
        aligned_groups(pd.Series([10, 20, 30, 40]), row_ids=ids, labels=labels)


@pytest.mark.parametrize('values,ids,labels', [
    ([1], [1, 2], ['A', 'B']), ([1, 2], [1, 2], ['A']),
    ([1, 2], [1, 2], ['A', None]), ([1, 2], [1, 1], ['A', 'A']),
    ([[1, 2]], [1], ['A']),
])
def test_grouping_rejects_missing_and_mismatched_observations(values, ids, labels):
    with pytest.raises(AnalysisInvariantError):
        aligned_groups(values, row_ids=ids, labels=labels)


def test_multiindex_groups_preserve_all_observations():
    ids = [('A', 1), ('B', 2)]
    groups = aligned_groups([4, 8], row_ids=ids, labels=[('P', 1), ('P', 2)])
    assert groups[('P', 1)].tolist() == [4]
    assert groups[('P', 2)].tolist() == [8]


def produce(work, parameters, input_versions):
    output = work / 'result.json'
    output.write_text(json.dumps({'value': parameters['value']}))
    return {'result': output}


def validate(outputs, contract):
    observed = json.loads(outputs['result'].read_text())['value']
    return {'passed': observed == contract['expected'], 'observed': observed}


def stage(cache, **kwargs):
    args = dict(input_versions={'selected_object': 'version-1'}, parameters={'value': 42},
                contract={'expected': 42}, produce=produce, validate=validate)
    args.update(kwargs)
    return cache.run('fit', **args)


def test_reuse_checks_bytes_and_contract_without_rewriting(tmp_path):
    cache = StageCache(tmp_path / 'stages')
    first = stage(cache)
    before = first.outputs['result'].stat().st_mtime_ns
    second = stage(cache)
    assert not first.reused and second.reused
    assert first.identity == second.identity and first.outputs == second.outputs
    assert second.outputs['result'].stat().st_mtime_ns == before


@pytest.mark.parametrize('changed', [
    {'input_versions': {'selected_object': 'version-2'}},
    {'parameters': {'value': 43}, 'contract': {'expected': 43}},
    {'contract': {'expected': 42, 'method_revision': 2}},
])
def test_changed_dependencies_cannot_reuse(tmp_path, changed):
    cache = StageCache(tmp_path / 'stages')
    first = stage(cache)
    second = stage(cache, **changed)
    assert not second.reused and first.identity != second.identity
    assert first.outputs['result'].exists()


def test_changed_helper_module_invalidates_stage(tmp_path):
    helper = tmp_path / 'method.py'
    helper.write_text('VERSION = 1\n')
    cache = StageCache(tmp_path / 'stages')
    first = stage(cache, source_files=[helper])
    helper.write_text('VERSION = 2\n')
    second = stage(cache, source_files=[helper])
    assert not second.reused and second.identity != first.identity


def test_failed_repair_never_replaces_successful_stage(tmp_path):
    cache = StageCache(tmp_path / 'stages')
    first = stage(cache)
    with pytest.raises(AnalysisInvariantError, match='did not explicitly pass'):
        stage(cache, parameters={'value': 0})
    assert stage(cache).reused
    assert json.loads(first.outputs['result'].read_text())['value'] == 42
    assert not [path for path in cache.root.glob('.fit-*') if path.is_dir()]


def test_tampered_cached_file_is_recomputed_and_new_result_is_reusable(tmp_path):
    cache = StageCache(tmp_path / 'stages')
    first = stage(cache)
    first.outputs['result'].write_text('{"value": 0}')
    second = stage(cache)
    assert not second.reused
    assert json.loads(second.outputs['result'].read_text())['value'] == 42
    assert stage(cache).reused
    assert len(list(cache.root.glob('.rejected-*'))) == 1


def mutate_validator(outputs, contract):
    outputs['result'].write_text('{"value": 999}')
    return {'passed': True}


def test_validator_cannot_change_output_and_claim_success(tmp_path):
    cache = StageCache(tmp_path / 'stages')
    with pytest.raises(AnalysisInvariantError, match='must not modify'):
        stage(cache, validate=mutate_validator)
    assert not list(cache.root.glob('fit-*'))


def external_producer(work, parameters, input_versions):
    return {'result': Path(parameters['path'])}


def symlink_producer(work, parameters, input_versions):
    (work / 'result.json').symlink_to(parameters['path'])
    return {'result': work / 'result.json'}


@pytest.mark.parametrize('producer', [external_producer, symlink_producer])
def test_source_file_references_are_not_stage_outputs(tmp_path, producer):
    original = tmp_path / 'original.json'
    original.write_text('{"value": 42}')
    cache = StageCache(tmp_path / 'stages')
    with pytest.raises(ValueError, match='inside'):
        stage(cache, produce=producer, parameters={'path': str(original)})
    assert original.read_text() == '{"value": 42}'


def test_stage_root_cannot_be_an_input_or_delivery_directory():
    for path in ['/home/ubuntu/inputs/cache', '/home/ubuntu/output/cache']:
        with pytest.raises(ValueError, match='separate'):
            StageCache(path)


def test_symlink_computation_directory_is_rejected(tmp_path):
    (tmp_path / 'real').mkdir()
    (tmp_path / 'link').symlink_to(tmp_path / 'real', target_is_directory=True)
    with pytest.raises(ValueError, match='symlinks'):
        StageCache(tmp_path / 'link' / 'cache')


def undeclared_producer(work, parameters, input_versions):
    (work / 'copied-source.csv').write_text('must not persist')
    return produce(work, parameters, input_versions)


def test_undeclared_work_files_are_not_committed_as_a_cache(tmp_path):
    cache = StageCache(tmp_path / 'stages')
    with pytest.raises(AnalysisInvariantError, match='Undeclared'):
        stage(cache, produce=undeclared_producer)
    assert not list(cache.root.rglob('copied-source.csv'))
    assert not list(cache.root.glob('fit-*'))


def downstream(cache, prior, **kwargs):
    args = dict(input_versions={}, upstream={'fit': prior}, parameters={'value': 1},
                contract={'expected': 1}, produce=produce, validate=validate)
    args.update(kwargs)
    return cache.run('report', **args)


def test_upstream_change_invalidates_downstream_without_recomputing_fit(tmp_path):
    cache = StageCache(tmp_path / 'stages')
    fit = stage(cache)
    first = downstream(cache, fit)
    assert downstream(cache, fit).reused
    with pytest.raises(AnalysisInvariantError):
        downstream(cache, fit, parameters={'value': 0})
    assert stage(cache).reused  # report failure never invalidates fitting
    changed = stage(cache, parameters={'value': 43}, contract={'expected': 43})
    second = downstream(cache, changed)
    assert not second.reused and first.identity != second.identity


def test_mutated_upstream_cannot_enter_downstream_cache(tmp_path):
    cache = StageCache(tmp_path / 'stages')
    fit = stage(cache)
    fit.outputs['result'].write_text('{"value": 999}')
    with pytest.raises(AnalysisInvariantError, match='Upstream'):
        downstream(cache, fit)
    assert not list(cache.root.glob('report-*'))


def test_callback_cannot_replace_the_upstream_receipt_bound_to_stage_identity(tmp_path):
    import hashlib
    cache = StageCache(tmp_path / 'stages')
    fit = stage(cache)
    upstream = {'fit': fit}

    def mutating_producer(work, parameters, input_versions):
        fit.outputs['result'].write_text('{"value": 999}')
        fit.output_sha256['result'] = hashlib.sha256(fit.outputs['result'].read_bytes()).hexdigest()
        upstream.clear()
        return produce(work, parameters, input_versions)

    with pytest.raises(AnalysisInvariantError, match='Upstream'):
        downstream(cache, fit, upstream=upstream, produce=mutating_producer)
    assert not list(cache.root.glob('report-*'))


def test_empty_upstream_does_not_invent_an_input_version(tmp_path):
    cache = StageCache(tmp_path / 'stages')
    with pytest.raises(ValueError, match='Declare immutable'):
        cache.run('report', input_versions={}, upstream={}, parameters={'value': 1},
                  contract={'expected': 1}, produce=produce, validate=validate)
