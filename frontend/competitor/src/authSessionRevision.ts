export class AuthSessionRevision {
  private value = 0;

  advance(): void {
    this.value += 1;
  }

  snapshot(): number {
    return this.value;
  }

  isCurrent(snapshot: number): boolean {
    return snapshot === this.value;
  }
}
