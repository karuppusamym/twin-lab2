# One workstation session, thirteen applications (the ten-app fixture)

Synthetic: no real person's data. `session/` is a recorder session folder as the collectors leave
it (session journal, one recording per channel, the capture journal with event ids), written by
`generate.py`. `../references-ten-apps.json` is what the recorder's own timeline
(`twinlab2_recorder.workstation.timeline.build`) builds from it: contract 1.2.0, every step with
its event id, completeness `complete`. `../references-ten-apps-1.1.json` is the same session
without the three finance applications, as the 1.1 recorder of PR 23 built it (first-segment
routes); it is kept unchanged for the portal's 1.1 tests. Regenerate the folder and the 1.2 references with:

    python tests/fixtures/ten-app-session/generate.py

`tests/recorder/test_ten_app_fixture.py` checks that the folder is the generator's output, that
the timeline still builds the committed references, and that they are in the recorder contract.
`tests/server/test_ten_apps.py` sends them to the portal and checks the thirteen packages.

| Tag | Surface | Identity the recorder sends | Registered as | Why it is here |
|---|---|---|---|---|
| billing | web | `web:https://erp.example.test` (path `/billing`) | Billing (catalog, id 1) | shares its origin with the next one |
| admin | web | same origin, path `/billing-admin` | Billing Admin (catalog, id 2) | its name contains "Billing"; its path starts with `/billing` |
| meter | web | `web:https://meter.example.test` | Zählerstand Portal – Nord (catalog, id 3) | spaces, umlaut, en dash |
| invoice | web | `web:https://seikyu.example.test:8443` | 請求 システム (catalog, id 4) | no Latin letters; a port |
| orders | web | `web:https://orders.example.test` | orders.example.test (discovered) | named by its host |
| hr | web | `web:http://127.0.0.1:8081` | 127.0.0.1:8081 (discovered) | an address with a port |
| contoso | desktop | `desktop:Contoso Ltd/Billing` (ContosoBilling.exe) | Billing (Contoso Ltd) (discovered) | window title "Billing", same name as the web Billing |
| plain | desktop | `desktop:exe/billing.exe` (no version resource) | Billing (billing.exe) (discovered) | window title "Billing", another executable |
| kasse | desktop | `desktop:Müller & Söhne GmbH/Kasse – Filiale Nord` | Kasse – Filiale Nord (discovered) | non-ASCII company and product, `&` |
| mainframe | mainframe | `mainframe:cics.example.test:3270` | Kontoführung CICS (discovered) | the TN3270 channel |
| ledger | web | `web:https://fin.example.test` (path `/finance`) | Finance Hub (catalog, id 5) | the shortest of three nested prefixes; one step at `/finance/billing-archive` |
| receivable | web | same origin, path `/finance/billing` | Accounts Receivable (catalog, id 6) | a two-segment prefix |
| payroll | web | same origin, path `/finance/payroll` | Payroll (catalog, id 7) | a two-segment prefix; a step URL with a query |

The recorder was offered a portal session (id 1) whose targets are the seven catalog
applications; the person added the others on the recorder. Each application's labels carry its
tag (`billingApprove`, `adminRateTable`, …), so a step that lands on the wrong application shows.
The steps of the applications alternate in time. Notepad was open (a presence row).
