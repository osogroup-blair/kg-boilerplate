## What and why

<!-- The behaviour change, and the reason. -->

## How I tested it

<!-- Commands you ran: make lint typecheck test, make test-int ..., make site-check. Say what you did NOT verify. -->

## Checklist

- [ ] `make lint typecheck test` passes
- [ ] API changed: `make openapi` run and the result committed
- [ ] Migration changed: tested with `make reset up seed`
- [ ] Model-affecting change (alias, price, prompt, ranker, Graphiti pin): live gate recorded
- [ ] Docs updated at the source (not the site's copy); `make site-check` passes if the site is affected
- [ ] No secrets, keys or personal data in the diff
