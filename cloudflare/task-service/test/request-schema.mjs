import assert from 'node:assert/strict';
import test from 'node:test';
import {REQUEST} from '../src/request-contract.mjs';
import {check} from '../src/validation.mjs';

test('REQUEST admits auto/omitted mode for scenarios and rejects omitting both mode and scenario', async () => {
  await check(REQUEST, {
    mode: 'agent',
    objective: 'review the change',
    output_schema: {type: 'object'},
    scenario: 'pr_code_review',
    complexity: 'standard',
  });
  await check(REQUEST, {
    mode: 'auto',
    objective: 'summarize research',
    output_schema: {type: 'object'},
    scenario: 'strategy_research_summary',
  });
  await check(REQUEST, {
    objective: 'summarize research',
    output_schema: {type: 'object'},
    scenario: 'strategy_research_summary',
  });
  await assert.rejects(
    () => check(REQUEST, {
      objective: 'missing both mode and scenario',
      output_schema: {type: 'object'},
    }),
    (err) => err.name === 'ValidationError',
  );
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
