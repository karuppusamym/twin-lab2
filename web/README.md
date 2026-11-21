# TwinLab2 web app

The React front end of the TwinLab2 portal, built only on the JSON API in
[`../api/openapi.yaml`](../api/openapi.yaml). The portal (FastAPI) serves `dist/` itself.

Vite, React 18, TypeScript (strict), React Router, TanStack Query. API types are generated from
the contract with `openapi-typescript` (`src/api/schema.ts`, committed; CI fails when it is stale)
and calls go through `openapi-fetch` (`src/api/client.ts`).

## Run

```sh
npm ci            # on a Windows machine whose npmrc forces os=linux: npm ci --os=win32
npm run dev       # http://localhost:5173; /api and /artifacts go to the portal at http://127.0.0.1:8765
                  # (start it first: python -m twinlab2.portal; TWINLAB2_PORTAL=http://host:port to change)
```

| Script | |
| --- | --- |
| `npm run gen` | regenerate `src/api/schema.ts` after changing `api/openapi.yaml` |
| `npm run lint` | eslint (typescript-eslint strict, react-hooks, jsx-a11y) |
| `npm run typecheck` | `tsc --noEmit` |
| `npm test` | Vitest + Testing Library; MSW answers every `/api/v1` call with the contract's examples |
| `npm run build` | `dist/`, a static app the portal (or any static server) serves |

## How it fits the portal

* **Same origin, relative URLs.** Every call goes to `/api/v1/...` with the session cookie, and
  every non-GET request sends `X-CSRF-Token` (from `GET /api/v1/session`). The same build works
  when the portal serves `dist/` and when it sits behind the OpenShift OAuth proxy.
* **Same page paths.** Routes keep the old page addresses (`/apps/1`, `/apps/1/build-pack/preview`,
  `/releases/5`, `/recordings/<id>` …), so bookmarks and the journey links the API returns work.
  The portal answers every other GET outside `/api`, `/artifacts` and `/assets` with `index.html`.
* **Roles.** Controls are shown or hidden from the caller's roles in `GET /api/v1/session`
  (`src/session.tsx` mirrors `auth.allowed`); the server enforces every rule. Restricted evidence
  (an application's evidence and scenarios) is not even requested unless the caller is an owner.
* **Downloads** are plain links to the API's download endpoints.
* **Errors** are the API's `problem+json`; pages show its `detail`.

## Tests

`test/contract.ts` reads the contract and builds MSW handlers from its examples;
`test/contract.test.ts` checks that the contract is valid OpenAPI 3.1, that every example matches
its schema, every error is problem+json and every operation states its role rule. The page tests
cover sign-in, role gating, the per-application package freeze flow, a recording holding several
applications (each with its own steps and package), the build chat, and a render of every page.
