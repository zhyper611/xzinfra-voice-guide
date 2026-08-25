(function exposePressGesture(root, factory) {
  const api = factory();
  if (typeof module === "object" && module && module.exports) {
    module.exports = api;
  } else if (root) {
    root.ShowroomPressGesture = api;
  }
}(typeof globalThis === "undefined" ? this : globalThis, () => {
  function createPressGesture({
    thresholdMs,
    onShortPress,
    onLongPress,
    onHoldStart = () => {},
    onHoldEnd = () => {},
    setTimer = setTimeout,
    clearTimer = clearTimeout,
  }) {
    let disabled = false;
    let pressing = false;
    let longPressTriggered = false;
    let timerId = null;

    function clearPendingTimer() {
      if (timerId === null) return;
      const pendingTimerId = timerId;
      timerId = null;
      clearTimer(pendingTimerId);
    }

    function start() {
      if (disabled || pressing) return;
      pressing = true;
      longPressTriggered = false;
      try {
        timerId = setTimer(() => {
          timerId = null;
          if (!pressing || disabled || longPressTriggered) return;
          longPressTriggered = true;
          onLongPress();
        }, thresholdMs);
        onHoldStart();
      } catch (error) {
        pressing = false;
        longPressTriggered = false;
        try {
          clearPendingTimer();
        } catch {
          timerId = null;
        }
        throw error;
      }
    }

    function end() {
      if (!pressing) return;
      pressing = false;
      clearPendingTimer();
      const emitShortPress = !disabled && !longPressTriggered;
      longPressTriggered = false;
      onHoldEnd();
      if (emitShortPress) onShortPress();
    }

    function cancel() {
      if (!pressing) return;
      pressing = false;
      clearPendingTimer();
      longPressTriggered = false;
      onHoldEnd();
    }

    function setDisabled(value) {
      const nextDisabled = Boolean(value);
      if (nextDisabled === disabled) return;
      disabled = nextDisabled;
      if (disabled) cancel();
    }

    return { start, end, cancel, setDisabled };
  }

  return { createPressGesture };
}));
