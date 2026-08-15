export interface FloatingChartTooltipPosition {
  x: number;
  y: number;
  viewportWidth: number;
  alignLeft: boolean;
  alignAbove: boolean;
}

function viewportSize() {
  return {
    width: Math.max(document.documentElement.clientWidth, window.innerWidth || 0),
    height: Math.max(document.documentElement.clientHeight, window.innerHeight || 0),
  };
}

export function floatingChartTooltipAt(
  clientX: number,
  clientY: number,
): FloatingChartTooltipPosition {
  const viewport = viewportSize();
  const x = Math.min(Math.max(clientX, 12), Math.max(12, viewport.width - 12));
  const y = Math.min(Math.max(clientY, 12), Math.max(12, viewport.height - 12));
  return {
    x,
    y,
    viewportWidth: viewport.width,
    alignLeft: x > viewport.width / 2,
    alignAbove: y > viewport.height / 2,
  };
}

export function floatingChartTooltipFromEvent(
  event: Event,
): FloatingChartTooltipPosition {
  if (event instanceof MouseEvent) {
    return floatingChartTooltipAt(event.clientX, event.clientY);
  }

  const target = event.currentTarget;
  if (target instanceof Element) {
    const bounds = target.getBoundingClientRect();
    return floatingChartTooltipAt(
      bounds.left + bounds.width / 2,
      bounds.top + bounds.height / 2,
    );
  }

  const viewport = viewportSize();
  return floatingChartTooltipAt(viewport.width / 2, viewport.height / 2);
}

export function floatingChartTooltipStyle(
  position: FloatingChartTooltipPosition,
  maximumWidth: number,
) {
  const width = Math.min(maximumWidth, Math.max(0, position.viewportWidth - 24));
  const preferredLeft = position.alignLeft
    ? position.x - width - 14
    : position.x + 14;
  const left = Math.min(
    Math.max(12, preferredLeft),
    Math.max(12, position.viewportWidth - width - 12),
  );
  return {
    left: `${left}px`,
    top: `${position.y}px`,
  };
}

export function floatingChartTooltipClasses(position: FloatingChartTooltipPosition) {
  return {
    "tooltip-align-left": position.alignLeft,
    "tooltip-align-above": position.alignAbove,
  };
}
