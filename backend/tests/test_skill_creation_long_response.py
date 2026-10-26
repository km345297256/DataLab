"""The real gateway changes strategy after a full-script output truncation."""
import copy
import json
from unittest.mock import Mock

import pytest
from langchain.messages import AIMessage

from app.domain.services.skills.skill_creation.models import HelperScript
from app.domain.utils.model_response import ModelIncompleteResponseError
from test_skill_creation_gateway import make_gateway, requests
from test_skill_creation_script_authoring import CONTRACT, REQUEST, PLAN, COUNT, MAIN


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['implementation','synthetic_script_repair','synthetic_build_repair'])
@pytest.mark.parametrize('failure', [
    AIMessage(content='{"content":"synthetic-truncated-private-fragment', response_metadata={'finish_reason':'length'}),
    ModelIncompleteResponseError(finish_reason='length'),
])
async def test_first_truncation_switches_to_units_without_repeating_the_whole_script(stage, failure):
    gateway, calls, _ = make_gateway([failure, PLAN, COUNT, MAIN])
    payload = {'script_request':copy.deepcopy(REQUEST),'method_contract':copy.deepcopy(CONTRACT)}
    if stage in {'synthetic_script_repair', 'synthetic_build_repair'}:
        payload.update(script={**{k:REQUEST[k] for k in ('path','purpose','invocation')},'content':'original synthetic program'})
    if stage == 'synthetic_script_repair':
        payload['frozen_validation_plan'] = {'scripts':[{'script_path':REQUEST['path'],'cases':[{'expected_exit_code':0}]}]}
    if stage == 'synthetic_build_repair':
        payload['build_failures'] = [{'script_path':REQUEST['path'],'case_id':'normal','exception_type':'KeyError'}]
    before=copy.deepcopy(payload)
    checked=Mock()
    result=await gateway.generate(HelperScript,stage,'Preserve every method.',payload,checked,contract=CONTRACT)
    observed=requests(calls)
    assert [row.get('authoring_phase') for row in observed] == [None,'plan','unit','unit']
    assert 'synthetic-truncated-private-fragment' not in json.dumps(observed)
    assert payload == before
    checked.assert_called_once_with(result)
    assert result.path == REQUEST['path']
    assert COUNT['content'] in result.content and MAIN['content'] in result.content
    if stage == 'synthetic_script_repair':
        assert all(row['frozen_validation_plan']==before['frozen_validation_plan'] for row in observed)
    if stage == 'synthetic_build_repair':
        assert all(row['build_failures']==before['build_failures'] for row in observed)
        assert all('frozen_validation_plan' not in row and 'observed_validation' not in row for row in observed)
