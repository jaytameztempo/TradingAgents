import { useCallback, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Job } from "./api";

/** Start a server job, track whether it is running, and refresh every query when it ends. */
export function useJob() {
  const qc = useQueryClient();
  const [job, setJob] = useState<Job | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [result, setResult] = useState<Job | null>(null);

  const start = useCallback(async (launch: () => Promise<Job>) => {
    setError(null);
    setResult(null);
    try {
      setJob(await launch());
      setRunning(true);
    } catch (e) {
      setError(e);
    }
  }, []);

  const onDone = useCallback((final: Job) => {
    setRunning(false);
    setResult(final);
    qc.invalidateQueries();
  }, [qc]);

  return { job, running, error, result, start, onDone };
}
