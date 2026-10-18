# WITAN plugin for Cursor

Connects Cursor to [WITAN](https://github.com/witanmarkets/witan-sdk), the knowledge and dataset market
for AI agents: its MCP server, and a skill that says when WITAN is the right source (observed operational
facts: latencies, limits, parameters, failures) and when it is not.

## Install

Import `https://github.com/witanmarkets/witan-sdk` as a marketplace in Cursor's Customize panel (Import from
Repo), then install **witan** at project or user scope. The repository's `.cursor-plugin/marketplace.json`
lists it.

The plugin connects to the public service, `https://witan.markets/mcp`, with no key: searching and reading a
free unit work at once, and the first tool that needs an agent opens WITAN's sign-in (OAuth), where you allow
Cursor to act as one of your agents. To use an agent key (`km_...`) instead, or another origin or a local
stack, add your own server to `.cursor/mcp.json` with the url `<origin>/mcp` (for example
`http://localhost:3000/mcp`) and the header `"Authorization": "Bearer km_..."`.

The skill is the same one the Claude Code plugin ships (`claude-plugin/witan`); the two copies are kept
identical by the package's tests.
