"""The research agents ("swarm"): one research_swarm tool call, or one
re-run turn, expanded into 1 to 30 tasks that run in a bounded thread
pool (design/swarm_backend.md).

- ``plan``: the deterministic expansion of a plan into categories and
  tasks, the cost estimate and the re-run scopes (pure functions);
- ``executor``: one execution (version) of a swarm, from the full part
  to the saved version row;
- ``context``: what the tasks of one execution share;
- ``reporter``: task and part events;
- ``limits``: the process-wide concurrency limits;
- ``prompts``: the analysis prompts, schemas and output models;
- ``workers``: the five task kinds.
"""
