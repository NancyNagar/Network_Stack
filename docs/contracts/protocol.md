# Contract: agent messaging protocol

Blackboard: `docs/STATUS.md` (owner = orchestrator). Agents never edit files owned by
another agent; they send a ticket to the orchestrator, which routes it.

| Owner          | Files                                                  |
|----------------|--------------------------------------------------------|
| orchestrator   | docs/, src/packet.py, src/tcp.py, README.md, Makefile  |
| network        | netcfg/*.sh, test/ (topology, netem, e2e, fairness)    |
| http-client    | src/client.py, client/run-client, src/test_client.py   |
| http-server    | src/server.py, server/run-server, src/test_server.py   |
| compliance     | read-only; writes docs/compliance.md                   |
| report         | report/                                                |

Ticket (JSON, sent in agent final reply or via SendMessage):
```json
{"id":"T-001","from":"network","to":"orchestrator","req":"T12",
 "test":"e2e_loss10","expected":"md5 match","actual":"stall at 412 KB",
 "evidence":"test/results/e2e_loss10/client.log:3381-3400","priority":"high"}
```
Reply: `{"id":"T-001","status":"fixed|wontfix|need-info","note":"..."}`
