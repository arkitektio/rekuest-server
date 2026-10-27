"""Shared GraphQL operation strings used across the GraphQL and integration tests.

Selection sets are deliberate supersets (e.g. agent ops also fetch ``connected``);
selecting an extra field is harmless and lets one constant serve every call site that
previously inlined a near-identical operation.
"""

ENSURE_AGENT = """
    mutation EnsureAgent($input: AgentInput!) {
        ensureAgent(input: $input) {
            id
            name
            connected
        }
    }
"""

GET_AGENT = """
    query GetAgent($id: ID!) {
        agent(id: $id) {
            id
            name
            connected
        }
    }
"""

GET_AGENTS = """
    query GetAgents {
        agents {
            id
            name
            connected
        }
    }
"""

DELETE_AGENT = """
    mutation DeleteAgent($input: DeleteAgentInput!) {
        deleteAgent(input: $input)
    }
"""

CREATE_BLOK = """
    mutation CreateBlok($input: CreateBlokInput!) {
        createBlok(input: $input) {
            id
            name
            description
            creator {
                sub
            }
        }
    }
"""

CREATE_DASHBOARD = """
    mutation CreateDashboard($input: CreateDashboardInput!) {
        createDashboard(input: $input) {
            id
            name
        }
    }
"""

UPDATE_BLOK = """
    mutation UpdateBlok($input: UpdateBlokInput!) {
        updateBlok(input: $input) {
            id
            name
            description
            vocab
            catalogs
            locals {
                key
                kind
                default
                persist
            }
            demo {
                deps
                args
                task
                queries
                subscriptions
            }
            module {
                name
            }
            dependencies {
                key
                optional
            }
            components {
                id
                component
            }
        }
    }
"""

DELETE_BLOK = """
    mutation DeleteBlok($input: DeleteBlokInput!) {
        deleteBlok(input: $input)
    }
"""

FORK_BLOK = """
    mutation ForkBlok($input: ForkBlokInput!) {
        forkBlok(input: $input) {
            id
            name
            provided
            componentTree
            dependencies {
                key
            }
        }
    }
"""

BLOKS = """
    query Bloks($filters: BlokFilter) {
        bloks(filters: $filters) {
            name
            provided
            app {
                identifier
            }
            providers {
                id
            }
        }
    }
"""

MATERIALIZE_BLOK = """
    mutation MaterializeBlok($input: MaterializeBlokInput!) {
        materializeBlok(input: $input) {
            id
            name
            agentMappings {
                key
                agent {
                    id
                }
            }
            dashboardPlacements {
                id
            }
        }
    }
"""

UPDATE_MATERIALIZED_BLOK = """
    mutation UpdateMaterializedBlok($input: UpdateMaterializedBlokInput!) {
        updateMaterializedBlok(input: $input) {
            id
            agentMappings {
                key
                agent {
                    id
                }
            }
        }
    }
"""

DELETE_MATERIALIZED_BLOK = """
    mutation DeleteMaterializedBlok($input: DeleteMaterializedBlokInput!) {
        deleteMaterializedBlok(input: $input)
    }
"""
