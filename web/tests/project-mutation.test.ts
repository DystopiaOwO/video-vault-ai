import { describe, expect, it } from "vitest";
import { createProjectMutationControls, ProjectMutationCoordinator, refreshFailureMessage } from "../src/projectMutation";

describe("ProjectMutationCoordinator", () => {
  it("blocks incompatible mutations in the same project", () => {
    const coordinator = new ProjectMutationCoordinator();
    const approve = coordinator.begin(7, "approve");

    expect(approve?.channel).toBe("review");
    expect(coordinator.begin(7, "reject")).toBeNull();
    expect(coordinator.begin(7, "render")).toBeNull();
  });

  it("keeps a newer project's busy token when an old operation finishes", () => {
    const coordinator = new ProjectMutationCoordinator();
    const oldOperation = coordinator.begin(7, "approve");
    coordinator.switchProject();
    const newOperation = coordinator.begin(8, "render");

    expect(oldOperation).not.toBeNull();
    expect(newOperation).not.toBeNull();
    coordinator.finish(oldOperation!);
    expect(coordinator.current()).toEqual(newOperation);
    expect(coordinator.isCurrent(oldOperation!, 8)).toBe(false);
  });

  it("formats a refresh failure without changing the mutation result", () => {
    expect(refreshFailureMessage("專案已核准", new Error("GET failed")))
      .toBe("專案已核准，但畫面更新失敗：GET failed");
    expect(refreshFailureMessage("正式輸出已排入佇列", new Error("GET failed"), "工作狀態"))
      .toBe("正式輸出已排入佇列，但工作狀態更新失敗：GET failed");
  });

  it("exposes shared mutation controls and current project state", () => {
    const coordinator = new ProjectMutationCoordinator();
    const controls = createProjectMutationControls(coordinator);
    const token = controls.beginProjectMutation(7, "color");

    expect(token?.mutation).toBe("color");
    expect(controls.isProjectMutationBusy(7)).toBe(true);
    expect(controls.isProjectMutationBusy(8)).toBe(false);
    expect(controls.currentProjectMutation(7)).toEqual(token);
    expect(controls.currentProjectMutation(8)).toBeNull();

    controls.finishProjectMutation(token!);
    expect(controls.currentProjectMutation()).toBeNull();
  });

  it("keeps a newer token when an older finally calls finish", () => {
    const coordinator = new ProjectMutationCoordinator();
    const controls = createProjectMutationControls(coordinator);
    const oldToken = controls.beginProjectMutation(7, "storyboard");
    coordinator.switchProject();
    const newToken = controls.beginProjectMutation(8, "render");

    controls.finishProjectMutation(oldToken!);
    expect(controls.currentProjectMutation()).toEqual(newToken);
  });
});
