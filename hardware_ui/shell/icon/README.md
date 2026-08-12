# Bundled application icon

`hardware-ui.svg` is Breeze's `devices/64/audio-card.svg`, copied **verbatim** — byte-identical to
the file shipped by `kde-frameworks/breeze-icons` 6.27.0, checked by sha256 rather than by eye.

**Why copied rather than referenced by name:** Breeze ships this artwork in colour only at 64px;
the 22px and 24px variants are monochrome outlines. A titlebar or task switcher requests a small
size and therefore gets the monochrome version. Installing the coloured SVG under our own icon
name makes it scale to any size and stay in colour.

## Licence

**LGPL-3.0-or-later** — *not* GPL, and not the same licence as the rest of this project.

> Copyright © 2014 Uri Herrera and others (the Breeze Icon Theme)

The full notice, with the authors' own contact details, is in `COPYING-ICONS` beside this file — verbatim, because that is the copy that counts.

LGPL-3 is GPL-3 plus additional permissions, so bundling it into this GPL-3 application is
permitted; the icon itself stays under the LGPL, and anyone is free to take it back out under those
terms. What the licence asks in return is that its text travel with the artwork, so both files that
matter are here rather than referenced:

| File | What it is |
|---|---|
| `COPYING-ICONS` | upstream's own notice, including the clarification about what "the library" means for artwork |
| `LICENSE.LGPL-3` | the LGPL-3 text itself |

Kept beside the icon in both locations it is installed from, because a licence that lives only in
one of two copies is a licence that goes missing the first time someone takes just the other.
