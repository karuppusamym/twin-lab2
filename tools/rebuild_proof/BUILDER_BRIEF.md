# The brief each blind builder got (fresh Claude subagent, nothing else)

`<folder>` is `<evidence>\builds\<case>-<app>`: an empty folder holding only the unzipped package. `<port>`
is the builder's own port for smoke checks (19101, 19111, 19121, 19131, 19141), so parallel builders don't
collide.

> You are a developer building an application from a build package, with nothing else to go on.
>
> Your folder: `<folder>`. It holds the unzipped package: BUILD_TASK.md (your task), evidence.json, …
>
> Rules (strict):
> 1. Read, list and search only inside your folder. Never look at any other path on this disk (no parent
>    folders, no home folder, no other projects, no installed site-packages source). No web search or web
>    fetch. Installing packages from PyPI is allowed, only into a virtual environment inside your folder;
>    prefer the Python standard library.
> 2. Never open a window: no browser tool or browser pane, no GUI programs. Start your server only in the
>    background for smoke checks, on 127.0.0.1 port `<port>`, and stop every process you started.
>    *(Desktop: build it with `dotnet build -c Release` but NEVER start the program; check with tests that
>    show no window.)*
> 3. Build the application BUILD_TASK.md describes in `<folder>\repo` (git init there and commit). It must
>    start from `<folder>\repo` with `python run.py`, reading PORT and any other settings BUILD_TASK.md
>    names *(desktop: `dotnet build -c Release` must produce CaseDesk.exe)*. Follow BUILD_TASK.md closely,
>    including its "Screens and controls" section; record assumptions in BUILD-NOTES.md.
> 4. When the app works and is committed, stop.
>
> Your final message: how to start it, what you built, and a list of every place where the package did not
> tell you something you needed (what you needed, where in the package you looked, what you assumed).
