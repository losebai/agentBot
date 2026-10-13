import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, vi } from 'vitest';

// jsdom 不支持滚动条伪元素样式；保留真实元素样式，让弹窗滚动锁定正常测试。
const readComputedStyle = window.getComputedStyle.bind(window);
window.getComputedStyle = (element: Element) => readComputedStyle(element);

Object.defineProperty(window, 'matchMedia', { value: vi.fn(() => ({
  matches: false, addListener() {}, removeListener() {},
  addEventListener() {}, removeEventListener() {}, dispatchEvent() { return false; },
})) });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
