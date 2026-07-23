type NativeKeyboardEventLike = {
  isComposing?: boolean;
  keyCode?: number;
};

export type EnterKeyboardEventLike = {
  key: string;
  isComposing?: boolean;
  keyCode?: number;
  nativeEvent?: NativeKeyboardEventLike;
};

/** Return true only for a committed Enter, never for an IME composition key. */
export function isCommittedEnter(event: EnterKeyboardEventLike): boolean {
  if (event.key !== "Enter") return false;
  return !(
    event.isComposing === true
    || event.keyCode === 229
    || event.nativeEvent?.isComposing === true
    || event.nativeEvent?.keyCode === 229
  );
}
