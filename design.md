# Video Vault AI Design System

## Purpose

This document is the UI visual source of truth for Video Vault AI. Product UI
styles must use the semantic tokens below instead of introducing one-off color
values in a component stylesheet. The token definitions live in
`web/src/app-shell.css` under `:root`; this document defines their intended
meaning and contrast requirements.

The design is a light workspace: a quiet blue-gray canvas, white working
surfaces, dark readable text, and one restrained blue action color. The dark
sidebar is the only intentionally dark navigation surface.

## Color tokens

### Global canvas and content

| Token | Value | Use |
| --- | --- | --- |
| `--app-bg` | `#f3f6fb` | Page background |
| `--app-surface` | `#ffffff` | Cards, panels, controls |
| `--app-surface-soft` | `#f8fafc` | Secondary panels and expanded details |
| `--app-border` | `#dfe6ef` | Standard separators and card borders |
| `--app-border-strong` | `#c9d3e1` | Interactive control borders |
| `--app-text` | `#182235` | Primary text and headings |
| `--app-muted` | `#68758a` | Supporting text and metadata |
| `--app-subtle` | `#8b97a9` | Low-emphasis labels only |

### Actions and states

| Token | Value | Use |
| --- | --- | --- |
| `--app-primary` | `#315efb` | Primary action, selected progress state, focus accent |
| `--app-primary-hover` | `#254bd2` | Hovered primary action |
| `--app-primary-soft` | `#edf2ff` | Hover background and soft primary emphasis |
| `--app-success` | `#16815a` | Successful action/state |
| `--app-danger` | `#b43b32` | Destructive/error action/state |

### Workspace navigation

Workspace navigation is light, so the selected item must never use white text.
These tokens are intentionally separate from dark-sidebar tokens:

| Token | Value | Use |
| --- | --- | --- |
| `--app-nav-text` | `#57667c` | Normal tab text |
| `--app-nav-hover-text` | `#244bcf` | Hover/focus tab text |
| `--app-nav-hover-bg` | `#edf2ff` | Hover/focus tab background |
| `--app-nav-active-text` | `#244bcf` | Selected tab text |
| `--app-nav-active-bg` | `#e8eeff` | Selected tab background |
| `--app-nav-active-border` | `#8ca7ff` | Selected tab border |
| `--app-nav-active-underline` | `#315efb` | Selected tab bottom accent |
| `--app-focus-ring` | `#315efb` | Keyboard focus ring |

`#244bcf` on `#e8eeff` is the required selected-tab contrast pair. Do not
replace the selected text with `#fff` or an undefined generic token such as
`--text`; that makes the active tab unreadable on the light navigation surface.

### Sidebar

| Token | Value | Use |
| --- | --- | --- |
| `--sidebar-bg` | `#111a2c` | Sidebar base |
| `--sidebar-surface` | `rgba(255, 255, 255, .065)` | Sidebar hover/active surface |
| `--sidebar-border` | `rgba(255, 255, 255, .105)` | Sidebar separators |
| `--sidebar-text` | `#dce5f3` | Sidebar primary text |
| `--sidebar-muted` | `#8f9db4` | Sidebar labels and metadata |

## Component rules

1. Use semantic tokens, not raw hex values, for new UI colors.
2. A light-surface active state uses dark primary text; a dark-surface active
   state uses light text. Never share a generic `--text` fallback between them.
3. Hover, active, and focus must remain distinguishable without relying only
   on color; borders or an underline should remain present.
4. Muted text is for metadata, never for primary labels or selected controls.
5. New status colors must provide both a foreground and a soft background pair.
6. If a new component needs a color role, add a semantic token here and to
   `web/src/app-shell.css` before using it.

## Regression guard

When changing navigation or global styles, check the selected workspace tab in
the light WebUI at normal and keyboard-focus states. The selected tab should
show dark blue text on a pale blue background with a visible blue underline.
Run the frontend tests and production build before handing the UI back for
review.
