# Taking this to Microsoft Fabric: notes from an exploration

*Written 2026-10-07. Exploration notes, not a design and not tested. Each claim says where it comes from.
"Verified" means a Microsoft or Anthropic page; "reported" means a third-party article or a search summary;
"unknown" means nobody has checked.*

## The question

dct-chat lets someone describe a dashboard in plain words and get a live, filterable board in the conversation.
Could the same idea be built as a **Microsoft Fabric app**, with **Rayfin** as the app framework instead of
dbt charts, and with Claude reached through a **Microsoft Foundry** key?

**Short answer: yes, in principle.** On-the-fly charts from a chat are straightforward with a JSON chart spec held
in UI state. The open questions are about plumbing (data access, auth, hosting the agent, data governance), not
about whether charts can be generated live.

## Two different ways to involve Fabric

1. **Fabric as a data source for what we have today.** Keep dct-chat and dbt charts; point a dbt project at a
   Fabric warehouse. This needs Fabric support in dbt charts, which the released 0.9.1 doesn't have. We filed
   [dbt-labs/dbt-charts#60](https://github.com/dbt-labs/dbt-charts/issues/60) and pushed a fix to the branch
   `fix/fabric-support` on the nickteff fork (four missing registry and error-classification entries; the upstream
   repo doesn't accept pull requests). dbt-fabric itself already connects.
2. **Rebuild as a Fabric app** (the rest of this document). A TypeScript web app on Rayfin that runs inside Fabric,
   next to the data.

## What Rayfin is

- **Verified ([Microsoft Learn](https://learn.microsoft.com/en-us/javascript/api/fabric-apps-sdk-javascript/rayfin-overview?view=fabric-apps-sdk-javascript-latest)):**
  an SDK and CLI. You define data models in TypeScript with decorators (`@entity`, `@text`, `@role`, ...) and get a
  database schema, GraphQL endpoints, generated typed clients and row-level security policies. `npx rayfin up`
  deploys it to Fabric.
- **Reported:** announced at Build 2026, public preview since 2 June 2026, no general-availability date, no
  separate price (it uses the Fabric capacity you already have), and app data lands in OneLake
  ([InfoWorld](https://www.infoworld.com/article/4181166/rayfin-signals-microsofts-push-to-make-fabric-an-ai-app-runtime.html),
  [Kanerika](https://kanerika.com/blogs/microsoft-rayfin/), [ITdaily](https://itdaily.com/news/cloud/microsoft-fabric-rayfin/)).
- **The SDK has no charts.** Its documentation describes only the backend. [Microsoft's Rayfin page](https://www.microsoft.com/en-us/microsoft-fabric/features/rayfin)
  shows dashboard screenshots without saying how they're built.

### The data app template (the dashboard layer)

- **Reported ([RADACAD](https://radacad.com/rayfin-data-app-the-future-of-power-bi-reporting/)):** a Rayfin "Data App"
  template, built with the Power BI team: a TypeScript and React frontend whose backend is a **Power BI semantic
  model**.
- **Reported ([Tabular Editor](https://tabulareditor.com/blog/fabric-apps-explained-visualization-as-code-in-a-data-app-dashboard)):**
  charts are written as code, with Vega-Lite as the recommended format and Microsoft helper packages (D3 is the
  alternative). Data comes from DAX queries kept in `.dax` files, run through the Execute Queries REST API. The
  framework is described as designed for AI coding agents to write those files.
- **Unknown:** we haven't opened the templates ([microsoft/awesome-rayfin](https://github.com/microsoft/awesome-rayfin))
  or the helper packages, so we don't know whether they can render a spec generated at runtime.

## How on-the-fly charts would work

The chart spec is data. The agent returns it, the app puts it in React state, and the chart redraws:

```tsx
const [cards, setCards] = useState<Card[]>([]);
onToolResult(({ title, spec, query }) => setCards(prev => [...prev, { id: newId(), title, spec, query }]));
{cards.map(c => <VegaEmbed key={c.id} spec={c.spec} />)}   // re-renders when a spec changes
```

- **No deployment per chart.** Render with Vega's own embedding library, so Microsoft's helper packages aren't
  required.
- **Edit a chart:** replace that card's spec in state ("make this a bar chart" is one update).
- **Filters:** re-run the card's query with the new value and update its data, or use Vega parameters.
- **Export and thumbnails:** Vega can produce PNG and SVG from a rendered chart; no server render.
- **Point at a chart:** Vega reports clicks and selections.
- **Check before showing:** Vega-Lite's compiler runs in Node, so the backend can compile each spec and send errors
  back to the agent, the same "render, check, fix" loop dct-chat has.
- **Trade-off:** dbt charts gave a polished house style and validation for free. With raw Vega-Lite, a theme and a
  good prompt have to supply that, or charts look generic.

### Data access (unconfirmed)

The likeliest route is DAX against a semantic model, called with the signed-in user's Entra token (on-behalf-of), so
row-level security applies to what the agent can see. Semantic models also carry defined measures, which addresses
dct-chat's weak spot of every board redefining "revenue". Not tried.

## What carries over from dct-chat, and what doesn't

| Carries over | Doesn't |
|---|---|
| The page design: chat, inline board cards, progress screen, SQL details view, export, boards sidebar (plain HTML and JS, ports to React components) | The Python server (FastAPI, the Python agent SDK) |
| The agent design: a few direct tools (query, render, docs), a validate-and-fix loop, per-user isolation | The dbt charts engine (Python), replaced by Vega-Lite which runs in the browser |
| Lessons: keep the prompt grounded in the schema, hide tool calls, drop mid-turn narration, confine what the agent can touch, keep heavy work in-process (a process per command made builds take minutes on a slow Windows PC) | The dbt project linking and manifest reading (replaced by the semantic model's own schema) |

## Reaching Claude through Microsoft Foundry

- **Verified ([Microsoft announcement](https://azure.microsoft.com/en-us/blog/claude-in-microsoft-foundry-is-now-generally-available/),
  [Claude docs](https://platform.claude.com/docs/en/build-with-claude/claude-in-microsoft-foundry)):** Claude is
  generally available in Microsoft Foundry (from 29 June 2026). Authentication is an API key from the Foundry portal
  or **Microsoft Entra ID**, with Azure role-based access. Inference is processed in Azure with a choice of Global or
  US data zones, and zero data retention is available. **Anthropic operates the inference and is the data
  processor and SLA provider**, so confirm the terms with whoever governs your data.
- **Verified ([Microsoft Learn](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/how-to/configure-claude-code),
  [Claude Code docs](https://code.claude.com/docs/en/azure-ai-foundry)):** Claude Code and the Agent SDK can use
  Foundry through environment variables: `CLAUDE_CODE_USE_FOUNDRY=1`, `ANTHROPIC_FOUNDRY_RESOURCE`, and
  `ANTHROPIC_FOUNDRY_API_KEY` (or Entra ID), with your own deployment names for Sonnet, Haiku and Opus. The
  Python and TypeScript SDKs support Foundry.
- **For the current Python app:** it should work by setting those variables (the app already passes its environment
  to the agent), but this is **untested**, and the model dropdown would need to map to your deployment names.
- **For a Fabric app, use the plain API with a small tool loop, not the Agent SDK.** The Agent SDK runs a bundled
  Claude Code process (about 110MB to download, a Git requirement on Windows, and several seconds before the first
  response). A chart-building agent doesn't need file tools. The tools are simple functions, so porting them is easy.
- **A Foundry key also removes two earlier worries:** a Claude Pro/Max plan has no API key, and the terms for
  running a subscription login behind a shared app were unclear.
- **Unknown:** whether every model and feature you need is offered on Foundry, and how quickly new ones arrive there.

## Open questions and risks

1. Can the Data App helper packages (or plain `vega-embed`) render a spec generated at runtime inside the app?
2. Rayfin hosting: long-running streaming requests, outbound calls to Foundry, where secrets live. It's a preview.
3. Executing DAX as the user (on-behalf-of) from a Rayfin backend: what is supported?
4. Governance: query results go to Claude. Approval is needed, and Anthropic is the data processor.
5. Style: a Vega-Lite theme and prompt that match what dbt charts gave us.

## Suggested next step: a small proof of concept

Not Fabric yet. A React and TypeScript page with a chat box, a Claude tool loop (through Foundry or the Anthropic
API), `run_query` over a sample dataset, and Vega-Lite cards held in state, with filter, edit-a-chart and export. It
would settle the rendering and agent-loop questions before any Fabric plumbing. Then, in order:
read the Rayfin templates; run the same loop against a semantic model with DAX; add Entra sign-in.

## Sources

- [Rayfin SDK overview (Microsoft Learn)](https://learn.microsoft.com/en-us/javascript/api/fabric-apps-sdk-javascript/rayfin-overview?view=fabric-apps-sdk-javascript-latest)
- [Rayfin (Microsoft Fabric)](https://www.microsoft.com/en-us/microsoft-fabric/features/rayfin)
- [Rayfin signals Microsoft's push to make Fabric an AI app runtime (InfoWorld)](https://www.infoworld.com/article/4181166/rayfin-signals-microsofts-push-to-make-fabric-an-ai-app-runtime.html)
- [Microsoft Rayfin (Kanerika)](https://kanerika.com/blogs/microsoft-rayfin/)
- [Microsoft launches Rayfin (ITdaily)](https://itdaily.com/news/cloud/microsoft-fabric-rayfin/)
- [Rayfin Data App (RADACAD)](https://radacad.com/rayfin-data-app-the-future-of-power-bi-reporting/)
- [Fabric Apps explained: visualization as code (Tabular Editor)](https://tabulareditor.com/blog/fabric-apps-explained-visualization-as-code-in-a-data-app-dashboard)
- [Community templates for Rayfin (GitHub)](https://github.com/microsoft/awesome-rayfin)
- [Claude in Microsoft Foundry is now generally available (Microsoft)](https://azure.microsoft.com/en-us/blog/claude-in-microsoft-foundry-is-now-generally-available/)
- [Claude in Microsoft Foundry (Claude docs)](https://platform.claude.com/docs/en/build-with-claude/claude-in-microsoft-foundry)
- [Configure Claude Code with Foundry (Microsoft Learn)](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/how-to/configure-claude-code)
- [Claude Code on Microsoft Foundry (Claude Code docs)](https://code.claude.com/docs/en/azure-ai-foundry)
- [Fabric Extensibility Toolkit overview (Microsoft Learn)](https://learn.microsoft.com/en-au/fabric/extensibility-toolkit/extensibility-toolkit-overview), a second route:
  custom items published into Fabric workspaces, as an iframe web app with Entra tokens. Not explored further here.
