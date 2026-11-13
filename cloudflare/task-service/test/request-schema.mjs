import assert from 'node:assert/strict';
import test from 'node:test';
import {REQUEST} from '../src/request-contract.mjs';
import {check} from '../src/validation.mjs';

test('REQUEST admits scenario submits without model and rejects unknown fields', async () => {
  await check(REQUEST, {
    mode: 'agent',
    objective: 'review the change',
    output_schema: {type: 'object'},
    scenario: 'pr_code_review',
    complexity: 'standard',
  });
  await assert.rejects(
    () => check(REQUEST, {
      mode: 'agent',
      objective: 'review the change',
      output_schema: {type: 'object'},
      scenario: 'pr_code_review',
      unexpected: true,
    }),
    (err) => err.name === 'ValidationError',
  );
});

test('REQUEST still accepts classic model submits', async () => {
  await check(REQUEST, {
    mode: 'api',
    objective: 'ping',
    model: 'configured-api',
    output_schema: {type: 'object'},
  });
});
