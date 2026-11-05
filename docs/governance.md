# PR governance

Every PR, including dependency updates, needs independent exact-head AI review
and all native required checks. High-risk source includes governance, gate and
AI-service paths. A head/PR/live-event-bound `agent-approved` or `owner-approved`
label clears that tier after review and checks. Agent clearance remains available
for all high-risk source and does not claim owner confirmation. Agents never
change `owner-approved` without a separate exact-candidate owner authorization.

The trusted base workflow reads `.github/governance/policy.json` at the immutable
PR base SHA. Only markers from a Bot through a listed Codex App are trusted; the
newest marker must be well-formed, completed and clean on this head. Candidate
policy edits cannot trust themselves. Missing evidence, changed evidence, a
shared open PR head, stale base ancestry or incomplete file/rename evidence blocks
clearance. The `ai-review` status must be required on the default branch.

The gate refreshes after Documentation check, Engineering governance,
QuantAIService V2 CI and Review gate, and on native comment/check/status events.
Base pushes, scheduled recovery and PR close/synchronize recovery use the same
serialized per-PR writer. Tests run actual embedded workflow code with fake
native APIs: `node --test scripts/test-risk-gate.cjs`. CI runs these tests in
QuantAIService V2 CI. Local checks also include the Python suites listed in
`AGENTS.md`, `python scripts/check_style.py --phase 2`, and actionlint.

## First adoption

The installed main gate still needs `owner-approved` for workflow source.
The first trust artifact is reviewed together with this change, using the App
identity already present on this repository's native Codex review comments.
Until it is merged into the immutable base, the candidate artifact grants no
trust. Source adoption does not configure an Environment or cloud identity.

## Live deployment boundary

Source merge is not deployment authority. Only deployment to live trading needs
owner authorization as the deployment touchpoint; actual Secrets/IAM changes and
destructive data/resource deletions still escalate to the owner. Removing a source
file is not by itself deletion of production data. Live trading, Kelly and all
stop-loss switches stay OFF. A label, Codex marker or green gate authorizes none
of these operations.

The following is a concrete **configuration sketch**, not an installed Environment
or a runnable deployer. Configure a fixed `live-trading` GitHub Environment using
one numeric owner reviewer ID, prevent self-review, disable administrator bypass,
and permit only protected branches:

```json
{
  "wait_timer": 0,
  "reviewers": [{"type": "User", "id": "<numeric-owner-id>"}],
  "prevent_self_review": true,
  "can_admins_bypass": false,
  "deployment_branch_policy": {
    "protected_branches": true,
    "custom_branch_policies": false
  }
}
```

Replace the placeholder with an actual number only in the authorized settings
operation. Verify plan/feature support first. The initiating agent must use its
own App/bot identity: a shared owner login cannot prove human approval and may
prevent the owner from approving their own run.

A trusted default-branch deployment workflow must use `workflow_dispatch`, accept
only a full candidate SHA, reject non-default-branch dispatch and arbitrary
targets/commands, and serialize deployment with `cancel-in-progress: false`.
Its credential-free preflight must validate main ancestry, independent review,
required checks, allowed target and verified artifact provenance/digest; dry-run
and display the exact target/revision/artifact/plan tuple before approval. The
live-mutating job must declare the following fixed boundary:

```yaml
if: github.ref == 'refs/heads/main'
environment:
  name: live-trading
permissions:
  contents: read
  id-token: write
```

After Environment approval, revalidate the frozen tuple; any change needs new
approval. Obtain only an environment-bound OIDC identity and deploy the verified
artifact through trusted workflow code. If local actions are used, checkout must
be pinned to the trusted workflow revision with `persist-credentials: false`.
Read back target revision/state afterward; unknown mutation results must not be
blindly retried. Approval is for the deployment scope, not each paper trade.

The cloud OIDC trust/role must bind repository, environment and the trusted
workflow identity/revision; no equivalent production secret or role may be
available outside the protected job. A required workflow policy check, executed
from independently protected trusted code, must reject missing/variable
Environments, production credentials/OIDC in other jobs, candidate-code execution
with production authority, extra triggers/targets and changes to the deployment
trust boundary. YAML alone cannot enforce cloud trust. Settings/trust writes must
remain outside implementation and merge agents' authority. Actual Secrets/IAM
changes and destructive deletion use similarly owner-protected scoped workflows.
This PR creates no Environment, cloud binding, workflow deployer or settings.
