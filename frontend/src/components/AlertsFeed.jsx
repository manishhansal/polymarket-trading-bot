import clsx from "clsx";
import { fmtTime } from "../lib/format";

const LEVEL = {
  info:        { dot: "bg-term-blue",   text: "text-term-blue"   },
  success:     { dot: "bg-term-green",  text: "text-term-green"  },
  warning:     { dot: "bg-term-amber",  text: "text-term-amber"  },
  error:       { dot: "bg-term-red",    text: "text-term-red"    },
  opportunity: { dot: "bg-term-purple", text: "text-term-purple" },
};

export default function AlertsFeed({ alerts }) {
  return (
    <div className="panel">
      <div className="panel-header">
        <span>▮ alerts</span>
        <span>{alerts.length}</span>
      </div>
      <div className="overflow-auto max-h-[320px] divide-y divide-border-subtle">
        {alerts.length === 0 ? (
          <div className="p-6 text-center text-term-gray text-xs">no alerts yet</div>
        ) : (
          alerts.map((a) => {
            const lv = LEVEL[a.level] || LEVEL.info;
            return (
              <div key={a.id} className="px-4 py-2 text-xs flex gap-3 items-start">
                <span className={clsx("mt-1 w-1.5 h-1.5 rounded-full shrink-0", lv.dot)} />
                <div className="flex-1 min-w-0">
                  <div className="flex justify-between gap-2">
                    <span className={clsx("font-semibold uppercase tracking-wider text-[10px]", lv.text)}>
                      {a.title}
                    </span>
                    <span className="text-term-gray text-[10px]">{fmtTime(a.timestamp)}</span>
                  </div>
                  <div className="text-term-gray mt-0.5 break-words">{a.message}</div>
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
