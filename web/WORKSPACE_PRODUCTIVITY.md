# Workspace productivity controls

The project workspace includes two global productivity helpers.

## Command palette

- Open with `Ctrl+K` on Windows/Linux or `Cmd+K` on macOS.
- Search commands with Traditional Chinese or English keywords.
- Move through results with Arrow Up/Down.
- Execute with Enter.
- Close with Escape or by clicking the backdrop.
- `Alt+1` through `Alt+6` remain direct workspace shortcuts and are not handled by the command palette.

The command palette is mounted only on the project workspace. The standalone BGM library does not show project-only commands.

## Unsaved draft summary

A fixed summary appears only when the current project has unsaved work. It covers:

- storyboard structure;
- timing and trim drafts;
- color settings;
- audio settings;
- review notes;
- clip-summary descriptions.

Each chip scrolls to and focuses the corresponding workspace. The summary disappears after all drafts are saved or reset.

## Validation

Run from `web`:

```powershell
npm test
npm run build
```

Manual checks should include desktop and mobile overlay placement, reduced-motion behavior, project switching protection, and command-palette focus restoration.
