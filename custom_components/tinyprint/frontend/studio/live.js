// Latest-wins render scheduler: at most one render per animation frame, never two in flight.

export class RenderScheduler {
  #run;
  #onResult;
  #onError;
  #raf;
  #generation = 0;
  #settled = 0;
  #build = null;
  #frameQueued = false;
  #running = false;
  #disposed = false;

  /**
   * @param run (input, generation) => Promise<result>; the result carries that generation
   * @param onResult called only for the newest generation
   * @param onError called with (error, generation) only for the newest generation
   */
  constructor(run, onResult, onError, raf = requestAnimationFrame) {
    this.#run = run;
    this.#onResult = onResult;
    this.#onError = onError;
    // Called detached: the browser's requestAnimationFrame throws when `this` is not the window.
    this.#raf = (callback) => raf(callback);
  }

  /** Asks for a render of what `buildInput()` returns when it starts; returns the new generation. */
  request(buildInput) {
    if (this.#disposed) return this.#generation;
    this.#generation += 1;
    this.#build = buildInput;
    this.#queueFrame();
    return this.#generation;
  }

  get pending() {
    return this.#settled < this.#generation;
  }

  get generation() {
    return this.#generation;
  }

  dispose() {
    this.#disposed = true;
    this.#build = null;
  }

  #queueFrame() {
    if (this.#frameQueued || this.#running || !this.#build) return;
    this.#frameQueued = true;
    this.#raf(() => {
      this.#frameQueued = false;
      this.#start();
    });
  }

  async #start() {
    if (this.#disposed || this.#running || !this.#build) return;
    const build = this.#build;
    const generation = this.#generation;
    this.#build = null;
    this.#running = true;
    try {
      const result = await this.#run(build(), generation);
      this.#finish(generation, () => this.#onResult(result));
    } catch (error) {
      this.#finish(generation, () => this.#onError(error, generation));
    }
  }

  #finish(generation, deliver) {
    this.#running = false;
    if (!this.#disposed && generation === this.#generation) {
      this.#settled = generation;
      deliver();
    }
    this.#queueFrame();
  }
}
