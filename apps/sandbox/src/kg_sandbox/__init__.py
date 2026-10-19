"""The public sandbox broker: hands each visitor a private, pre-seeded workspace on a mock-router instance for a couple of hours, then deletes it.

It uses the platform only from outside (HTTP, the SDK, the `kg workspaces create` CLI that makes a workspace) and the example kit, like every other example.
It is deliberately small: sessions live in one SQLite file, and everything privileged about a workspace is created through the same governed paths a real
operator uses (workspace creation, principal registration, the workspace deletion request)."""
