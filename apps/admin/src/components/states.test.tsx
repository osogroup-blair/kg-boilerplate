import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Empty, ErrorState, Loading, NotFoundState, PermissionDenied, stateForStatus } from "./states";

describe("shared states", () => {
  it("renders loading, empty, permission-denied, not-found and error states", () => {
    render(<><Loading /><Empty title="Nothing here" hint="Add one" /><PermissionDenied what="the audit log" /><NotFoundState what="This principal" /><ErrorState message="boom" /></>);
    expect(screen.getByRole("status")).toHaveTextContent("Loading");
    expect(screen.getByText("Nothing here")).toBeInTheDocument();
    expect(screen.getByText(/Your role cannot view the audit log/)).toBeInTheDocument();
    expect(screen.getByText(/This principal does not exist/)).toBeInTheDocument();
    expect(screen.getByText("boom")).toBeInTheDocument();
  });

  it("maps API statuses to states", () => {
    expect(stateForStatus(200, "x")).toBeNull();
    expect(stateForStatus(undefined, "x")).toBeNull();
    for (const status of [403, 404, 500]) expect(stateForStatus(status, "x")).not.toBeNull();
  });
});
