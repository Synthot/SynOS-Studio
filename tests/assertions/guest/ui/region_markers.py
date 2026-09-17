"""Shell markers that prove GNOME runs in the acceptance Live language.

Shared by the guest probe (observe_installed_region) and the host validator
(_validate_installed_region_ui_events). No imports: this file is uploaded
into the guest as-is. Add a language here to let installs in that language
pass the installed-region contract; the desktop suites additionally need the
label table in core.py.
"""

REGION_MARKERS = {
    "zh": [("menu", "系统"), ("toggle button", "显示应用")],
    "en": [("menu", "System"), ("toggle button", "Show Applications")],
    "fr": [("menu", "Système"), ("toggle button", "Afficher les applications")],
    "de": [("menu", "System"), ("toggle button", "Anwendungen anzeigen")],
    "nl": [("menu", "Systeem"), ("toggle button", "Toepassingen tonen")],
}


def markers_for(language: str) -> list[tuple[str, str]]:
    try:
        return list(REGION_MARKERS[language])
    except KeyError:
        raise KeyError(
            f"no GNOME Shell region markers for language {language!r}; "
            "add them to tests/assertions/guest/ui/region_markers.py"
        ) from None
