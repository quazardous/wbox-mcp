# Driving LibreOffice Writer from Claude

Register this directory and ask for something ordinary:

```bash
cd examples/libreoffice-writer/
wboxr init --register     # or point your own .mcp.json at this config.yaml
```

> Open Writer, type "Hello from wbox", and show me a screenshot.

Claude calls `launch`, then `type_text`, then `screenshot`. With
`headless: true` nothing appears on your desktop while it happens — you can
keep working in the window you're reading this in.

## What to expect

The first `launch` is slow. LibreOffice takes tens of seconds to come up on a
cold profile, which is why `timeouts.app_render` in `config.yaml` is nowhere
near the 3-second default. If you get `app did not render`, raise it rather
than assuming wbox is broken.

`expected.png` is what a successful run looks like: Writer, undecorated, with
typed text in the document and the word count in the status bar reflecting it.

## Things worth trying next

- `key("ctrl+b")` then `type_text(...)` — formatting works like any keystroke.
- `click(x, y)` on a toolbar button, after a `screenshot` to find it.
- `clipboard_write("...")` then `key("ctrl+v")` — useful for text with
  characters that are awkward to type.
- `list_windows` when a modal dialog appears and you can't see it: Writer's
  dialogs are separate toplevels.

## If the document comes out garbled

`keyboard_layout: us` in `config.yaml` is not optional on a non-US host.
Without it the nested seat inherits your layout while wbox sends US keycode
positions, and `type_text("abc123")` arrives as `qbc!@#`.
