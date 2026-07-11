// The shape of the production bug that motivated this library, in Node.
//
// `level` gates the deck, and at level 0 it excludes the whole corpus — so the status claims the
// corpus is finished while every item in it remains unstudied. The OUTPUT is perfectly
// self-consistent. Nothing about it is wrong on its face. Only an internal variable is.

export function makeTools(store) {
  return {
    async studyStatus({ email, level = 1 }) {
      const rows = await store.get(`corpus:${email}`);
      const corpus = rows ?? [];
      const deck = corpus.filter((c) => c.x <= level);
      const done = deck.length === 0;
      return { corpus: corpus.length, deck: deck.length, done };
    },
  };
}
