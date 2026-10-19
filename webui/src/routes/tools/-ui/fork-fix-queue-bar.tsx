import type { ForkFixQueue } from '../-tools.fork-queue';

/** fork: what is waiting to run after the current Fix all, with a way to drop entries. */
export function ForkFixQueueBar({ queue }: { queue: ForkFixQueue }) {
  const { items, problems } = queue.state;
  if (!items.length && !problems.length) return null;
  return (
    <div className="repair-findings-bulk fork-fix-queue" id="fork-fix-queue">
      {items.length ? (
        <>
          <span className="repair-bulk-count">Queued:</span>
          {items.map((item) => (
            <span key={item.id} className="fork-fix-queue-item">
              {item.label} ({item.count.toLocaleString()})
              <button
                type="button"
                className="fork-fix-queue-x"
                aria-label={`Remove ${item.label} from the queue`}
                title="Remove from the queue"
                onClick={() => void queue.remove(item.id)}
              >
                &times;
              </button>
            </span>
          ))}
          {items.length > 1 ? (
            <button
              type="button"
              className="btn btn--sm btn--secondary"
              onClick={() => void queue.remove()}
            >
              Clear queue
            </button>
          ) : null}
        </>
      ) : null}
      {problems.map((problem) => (
        <span key={`p${problem.id}`} className="fork-fix-queue-problem">
          {problem.label} skipped: {problem.error}
        </span>
      ))}
    </div>
  );
}
