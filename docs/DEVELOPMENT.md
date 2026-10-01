# Development Guide

> **Architecture / design reference:** for *how the service is structured and why*, read the design
> docs in [`design/`](design/README.md). This guide covers environment setup and the contributor
> workflow.

## Getting Started

This guide will help you set up a development environment for Rekuest Server and understand the codebase structure.

The repository holds two programs: the rekuest server (Python/Django, the repository root) and
agentd (Rust, `agentd/`), which serves the whole agent protocol. Most of this guide is about the
server; [Working on agentd](#working-on-agentd) covers the other half.

## Prerequisites

- Python 3.12 or higher
- PostgreSQL 13+ (or SQLite for local development)
- Redis 6.0+
- Git
- Docker (the test suites bring their own Postgres and redis up in containers)
- A Rust toolchain (stable), only if you work on `agentd/`

## Development Setup

### 1. Clone and Install

```bash
# Clone the repository
git clone https://github.com/arkitektio/rekuest-server-next.git
cd rekuest-server-next

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install development dependencies
pip install -e ".[dev]"
```

### 2. Environment Configuration

The checked-in [`config.yaml`](../config.yaml) holds development values (database `db`, redis
`redis`, a development instance key). Edit it, or override single values from the environment
(`POSTGRES__HOST=localhost`, …). Every key is documented in [`CONFIG.md`](../CONFIG.md).

The server needs agentd for anything that touches an agent or a task: set `rekuest.agentd_url`
(or `REKUEST__AGENTD_URL`) to a running agentd. agentd reads the same file.

### 3. Database Setup

```bash
# Run migrations
python manage.py migrate

# Create a superuser (optional)
python manage.py createsuperuser
```

### 4. Start Development Server

```bash
# Start the development server
python manage.py runserver

# Or use the debug script
./run-debug.sh
```

The server will be available at `http://localhost:8000`

## Project Structure

```
rekuest-server-next/
├── config.yaml              # Configuration file, read by the server and by agentd
├── docker-compose.yaml      # The pair for local use: db, redis, server, background loop, agentd
├── manage.py                # Django management script
├── pyproject.toml           # Project dependencies and settings
├── run.sh                   # Web process: migrate, then daphne
├── run-reaper.sh            # Background loop: python manage.py reaper
├── facade/                  # Main application package
│   ├── models/             # Database models (package: caller, agent, action, …)
│   ├── schema.py           # GraphQL schema definition (Query/Mutation/Subscription)
│   ├── types/              # GraphQL types (package)
│   ├── inputs/             # GraphQL input types (package)
│   ├── filters/            # Query filters + org/auth scoping
│   ├── agentd.py           # Client of agentd's internal API (assign, control, agents, probes)
│   ├── backend.py          # The control backend the mutations call; delegates to agentd.py
│   ├── schedules.py        # Schedule calls to agentd (validate, plan, run now)
│   ├── triggers.py         # What a trigger is checked against when it is written
│   ├── service_agents.py   # Provisions this hub's services as HookAgents
│   ├── reaper.py           # The background loop: service agents + stale embeddings
│   ├── descriptors.py      # requires/provides → JSONPath compiler
│   ├── managers.py         # Relational port-matching engine
│   ├── channels.py         # Realtime channels
│   ├── channel_events.py   # Channel payloads
│   ├── signals.py          # Model signals → channel broadcasts
│   ├── mutations/          # GraphQL mutations
│   ├── queries/            # GraphQL queries
│   ├── subscriptions/      # GraphQL subscriptions
│   └── migrations/         # Database migrations
├── rekuest/                # Django project settings + ASGI entrypoint
│   ├── configuration.py    # The typed schema of config.yaml
│   ├── settings.py         # Main settings (loaded from config.yaml)
│   ├── settings_test.py    # Test settings
│   ├── asgi.py             # ASGI app: GraphQL (HTTP and subscriptions) only
│   └── urls.py             # URL routing (admin, JWKS, health)
├── rekuest_core/           # Shared core: enums, inputs, scalars, objects
├── rekuest_service/        # Vendored package the hub's services use to act as HookAgents
├── embeddings/             # Semantic search: the model, the pgvector column, the healer
├── datalayer/              # Secondary app: media/data layer
├── tests/                  # The server's test suite
├── agentd/                 # agentd (Rust): the agent protocol
│   ├── crates/             # rekuest-server (the binary), facade, rekuest-server-core, authentikate, kante
│   ├── conformance/        # Black-box pytest suite against the pair
│   ├── docs/               # Agent protocol, task lifecycle, journal, workflows, provenance
│   ├── scripts/test-db.sh  # A Postgres migrated by this checkout, for cargo test
│   └── schema-migrations.txt  # The migrations agentd's SQL was written against
└── docs/                   # Documentation (see docs/design/ for architecture)
```

### Where the agent path lives

The server has no agent code. `rekuest/asgi.py` serves GraphQL only; there is no `/agi` route.
The agent websocket, the HookAgent and signal intakes, registration, assign and control, probes
and every sweep (deadlines, workflow resume, schedules, triggers, retention) are agentd's, under
`agentd/crates/facade/src/`.

A mutation that touches a task or an agent calls agentd's internal API through
`facade/agentd.py` (`POST <rekuest.agentd_url>/internal/<op>`, signed with the instance key).
The route table is at the top of `agentd/crates/rekuest-server/src/internal.rs`.

The server owns the schema. agentd writes the same tables with its own SQL, so two tests guard
the contract from this side:

- `tests/models/test_database_defaults.py`: every defaulted column has its default in the
  database (`db_default`), because agentd's inserts do not pass through Django.
- `tests/test_agentd_contract.py`: `agentd/schema-migrations.txt` names the latest migration of
  each app agentd depends on. A new migration fails this test until agentd's SQL has been
  checked against it and the file updated.

## Architecture Overview

> A deeper, narrative version of this section (with diagrams) lives in
> [`design/`](design/README.md). The summary below is enough to start contributing.

### GraphQL Schema

The API is built using Strawberry GraphQL with Django integration:

- **Types**: Defined in the `facade/types/` package, represent data structures
- **Queries**: Read operations in `facade/queries/`
- **Mutations**: Write operations in `facade/mutations/`
- **Subscriptions**: Real-time updates in `facade/subscriptions/`

### Database Models

Core models in the `facade/models/` package:

- **Caller**: The `(client, user, organization)` requestor identity — who asks for work
  (`models/caller.py`). See [`design/identity.md`](design/identity.md).
- **Agent**: The provider runtime — a connected client that executes work (`models/agent.py`).
- **Action**: Abstract, versioned task/function contracts.
- **Implementation**: Concrete realizations of actions by agents.
- **State / Patch / Snapshot**: Agent state, its incremental history, and checkpoints.
- **Task / TaskEvent**: the unit of work and its append-only execution log.

### Authentication

Uses the Authentikate system:
- JWT token-based authentication
- Client registration and management
- User and organization scoping
- Permission-based access control

## Development Workflow

### Making Changes

1. **Create a branch**: `git checkout -b feature/your-feature-name`
2. **Make changes**: Edit code, add tests, update documentation
3. **Run tests**: `python -m pytest`
4. **Check linting**: `ruff check .`
5. **Format code**: `ruff format .`
6. **Commit changes**: `git commit -m "Description of changes"`
7. **Push and create PR**: `git push origin feature/your-feature-name`

### Adding New Features

#### Adding a New GraphQL Query

1. **Define the function** in appropriate file under `facade/queries/`:
```python
def my_new_query(info: Info, param: str) -> types.MyType:
    # Query logic here
    return result
```

2. **Add to schema** in `facade/schema.py`:
```python
@strawberry.type
class Query:
    my_new_query = field(resolver=queries.my_new_query, description="Description")
```

3. **Add tests** in `tests/`:
```python
async def test_my_new_query(self, authenticated_context):
    query = """
        query MyNewQuery($param: String!) {
            myNewQuery(param: $param) {
                id
                field
            }
        }
    """
    # Test implementation
```

#### Adding a New Model

1. **Define the model** in the `facade/models/` package (add a module and export it from
   `facade/models/__init__.py`):
```python
class MyNewModel(models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
```

2. **Create migration**:
```bash
python manage.py makemigrations
python manage.py migrate
```

3. **Add GraphQL type** in the `facade/types/` package:
```python
@strawberry_django.type(MyNewModel)
class MyNewType:
    id: strawberry.ID
    name: str
    description: str
```

### Testing

The server's suite does not start agentd. Tests that reach the agent path use the `fake_agentd`
fixture (`tests/conftest.py`), which answers the internal API in-process from
`tests/agentd_fake.py`. agentd's real behaviour is tested in `agentd/`.

#### Running Tests

```bash
# Run all tests
python -m pytest

# Run specific test file
python -m pytest tests/test_graphql_queries.py

# Run with coverage
python -m pytest --cov=facade

# Run tests in parallel
python -m pytest -n auto
```

#### Writing Tests

- **Unit tests**: Test individual functions and models
- **Integration tests**: Test complete workflows
- **GraphQL tests**: Test API endpoints
- **Model tests**: Test database interactions

Example test structure:
```python
@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestMyFeature:
    async def test_my_functionality(self, authenticated_context):
        # Test implementation
        pass
```

### Code Quality

#### Linting and Formatting

```bash
# Check code style
ruff check .

# Format code
ruff format .

# Type checking
mypy facade/
```

#### Pre-commit Hooks

Install pre-commit hooks to automatically check code quality:

```bash
pre-commit install
```

### Debugging

#### GraphQL Playground

Visit `http://localhost:8000/graphql` for interactive query testing.

#### Django Debug Toolbar

Enable in development settings for SQL query analysis and performance profiling.

#### Logging

Configure logging in `settings.py`:
```python
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
    },
    'loggers': {
        'facade': {
            'handlers': ['console'],
            'level': 'DEBUG',
        },
    },
}
```

## Performance Optimization

### Database Optimization

- Use `select_related()` and `prefetch_related()` for efficient joins
- Add database indexes for frequently queried fields
- Monitor query performance with Django Debug Toolbar

### GraphQL Optimization

- Use Strawberry's built-in query optimization
- Implement field-level permissions efficiently
- Consider query complexity analysis for expensive operations

### Caching

- Redis for session and query result caching
- Database query result caching
- Agent state caching for quick lookups

## Deployment

### Docker Development

```bash
# The pair from this checkout: db, redis, the server, its background loop and agentd
docker compose up --build
```

GraphQL is at `http://localhost:8234/graphql`, the agent websocket at `ws://localhost:8235/agi`
(see [`docker-compose.yaml`](../docker-compose.yaml)).

### Production Considerations

- Use PostgreSQL instead of SQLite
- Configure Redis for session storage
- Set up proper logging and monitoring
- Use environment variables for secrets
- Enable HTTPS and security headers

## Working on agentd

agentd is a Cargo workspace under `agentd/`; see [`agentd/README.md`](../agentd/README.md).

```bash
cd agentd
eval "$(scripts/test-db.sh)"   # Postgres + redis, migrated by this checkout's server
cargo test
cargo clippy --all-targets -- -D warnings
scripts/test-db.sh down

cd conformance && uv run pytest   # real sockets against both images built from this checkout
```

Changing a model that agentd reads or writes means changing both sides in one commit: the
migration, agentd's SQL, and `agentd/schema-migrations.txt`. Changing an input model in
`rekuest_core/` means regenerating agentd's fixtures
(`agentd/crates/rekuest-server-core/tests/fixtures/generate_declarations.py`); CI's contract job
fails when they differ.

## Contributing Guidelines

### Code Style

- Follow PEP 8 for Python code
- Use type hints throughout the codebase
- Write descriptive docstrings for all functions and classes
- Keep functions small and focused

### Documentation

- Update API documentation for new endpoints
- Add inline comments for complex logic
- Update this development guide for new processes
- Include examples in docstrings

### Testing Requirements

- All new features must include tests
- Maintain test coverage above 80%
- Include both positive and negative test cases
- Test error handling and edge cases

### Pull Request Process

1. **Description**: Provide clear description of changes
2. **Tests**: Include comprehensive test coverage
3. **Documentation**: Update relevant documentation
4. **Review**: Address feedback from code reviews
5. **CI/CD**: Ensure all checks pass

## Common Issues and Solutions

### Database Migration Issues

```bash
# Reset migrations (development only)
python manage.py migrate facade zero
python manage.py makemigrations facade
python manage.py migrate

# Show migration status
python manage.py showmigrations
```

### GraphQL Schema Issues

```bash
# Validate schema
python manage.py graphql_schema --print

# Test specific query
python manage.py shell
>>> from facade.schema import schema
>>> result = schema.execute_sync("{ agents { id } }")
```

### Redis Connection Issues

```bash
# Test Redis connection
redis-cli ping

# Check Redis configuration in config.yaml
# Ensure Redis server is running
```

## Resources

- [Django Documentation](https://docs.djangoproject.com/)
- [Strawberry GraphQL Documentation](https://strawberry.rocks/)
- [PostgreSQL Documentation](https://www.postgresql.org/docs/)
- [Redis Documentation](https://redis.io/documentation)
- [Pytest Documentation](https://docs.pytest.org/)

## Getting Help

- **Issues**: Create GitHub issues for bugs and feature requests
- **Discussions**: Use GitHub Discussions for questions
- **Discord**: Join the Arkitekt Discord server
- **Documentation**: Check the online documentation
