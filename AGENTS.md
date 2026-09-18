# Documentation and Comment Guidelines

These guidelines apply to documentation, code comments, and task descriptions throughout the repository.

## Conciseness and Relevance

- Include only what readers need to understand and carry out the task.
- Keep documentation focused on current behavior, usage, and prerequisites.
- Use comments to explain intent, constraints, and rationale that are not clear from the code alone.
- Consolidate repeated explanations in one place and reference them where needed.
- Record change history in commit messages rather than documentation or comments.
- State the main point directly, using phrases such as "is," "does," and "supports."

## README Scope

- Keep README.md a short entry point: project purpose, prerequisites, a working quick start, and links to further documentation.
- Use clear headings and short paragraphs or lists so readers can find the first steps quickly.
- Put configuration references, advanced usage, troubleshooting, and test instructions in focused documents under docs/.
- Link to the authoritative configuration or guide instead of repeating version numbers, defaults, or detailed explanations.
- When updating the README, keep only what a first-time user needs to understand the project and start using it.

## Documentation Ownership

- Keep docs/aiperf.md focused on the AIPerf client: setup, load generation, measurement, results, and client troubleshooting.
- Keep docs/transformers-api.md limited to behavior and usage shared by base and enhanced: deployment, endpoints, requests, responses, and common settings.
- Put implementation-specific architecture, scheduling, configuration, and tests in separate guides such as docs/transformers-api-enhanced.md. Link between guides instead of duplicating their contents.
