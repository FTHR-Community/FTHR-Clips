# Settings layout and discovery

The settings surface keeps the existing dark theme, accent, custom typography,
control bindings, and lazy category construction.

- Categories use larger text-only tabs beside the search field. At narrow
  widths the category strip scrolls while search remains visible.
- Search results are grouped by category with result counts. Click a category
  or use Enter/Space to expand or collapse it; Left/Right retain native tree
  navigation. Collapse choices survive query refinements. Enter on a setting
  opens its category, scrolls to the control, focuses it, and adds a static accent
  outline and tint. The highlight clears on a new search or navigation and never
  blocks interaction. Escape clears search.
- General pairs system/notifications with uploads, settings presets with export
  presets, and troubleshooting with credits.
- Storage sits beside Clip and contains clip and recording folders, replay buffer,
  storage limits, and library backup/restore. Search destinations follow the move.
- Clip pairs encoding with editor/watermark preferences and keeps clip imports below.
- Pairs stack vertically when their minimum content widths no longer fit.
  Sections use the existing theme font rather than replacing user typography.
- Interface navigation and search emphasis are immediate, without animation.
  The former Reduce interface animations option has been removed.
- Performance includes a searchable Ultra performance mode switch. The runtime
  owns mode persistence and synchronizes the switch after accepting a transition.

Validation: rendered General, Clip, Performance and grouped search using real
Qt widgets at 1180×760 and 800×640. Existing search, transitions, storage,
audio/camera settings, popup layout, theme, and uploader consent tests passed.
The render used isolated settings widgets; native GPU capture is a separate
runtime check.
