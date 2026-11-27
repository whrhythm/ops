// pdfjs-polyfill.ts
var g = globalThis;
g.DOMMatrix ??= class DOMMatrix {
};
g.ImageData ??= class ImageData {
};
g.Path2D ??= class Path2D {
};
