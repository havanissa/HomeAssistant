// Undo / redo: snapshots of the whole plan (plans are small), with a label per step.
import { clonePlan, samePlan, type Plan } from "./model";

export interface Step {
  label: string;
  plan: Plan;
}

export class History {
  private past: Step[] = [];
  private future: Step[] = [];

  constructor(readonly limit = 200) {}

  /** Record `before` as the state to return to when the edit `label` is undone. */
  push(before: Plan, label: string): void {
    const last = this.past[this.past.length - 1];
    if (last && samePlan(last.plan, before) && last.label === label) return;
    this.past.push({ label, plan: clonePlan(before) });
    if (this.past.length > this.limit) this.past.shift();
    this.future = [];
  }

  get canUndo(): boolean {
    return this.past.length > 0;
  }

  get canRedo(): boolean {
    return this.future.length > 0;
  }

  get undoLabel(): string | null {
    return this.past[this.past.length - 1]?.label ?? null;
  }

  get redoLabel(): string | null {
    return this.future[this.future.length - 1]?.label ?? null;
  }

  /** The plan to show after undoing; `current` becomes redoable. */
  undo(current: Plan): Plan | null {
    const step = this.past.pop();
    if (!step) return null;
    this.future.push({ label: step.label, plan: clonePlan(current) });
    return clonePlan(step.plan);
  }

  redo(current: Plan): Plan | null {
    const step = this.future.pop();
    if (!step) return null;
    this.past.push({ label: step.label, plan: clonePlan(current) });
    return clonePlan(step.plan);
  }

  clear(): void {
    this.past = [];
    this.future = [];
  }
}
