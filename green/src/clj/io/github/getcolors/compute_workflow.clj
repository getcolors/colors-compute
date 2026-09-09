(ns io.github.getcolors.compute-workflow
  "Green SDK orchestration over the common single-node callback."
  (:require [green.workflow :as workflow]
            [io.github.getcolors.compute :as compute]))

(defn cluster-workflow
  "Construct an SDK workflow from ordered expanded requests and an entry id.

  node-step is a standard Green step: opts -> opts. Its individual request is
  under :colors-compute/request; normalized output is :colors-compute/params.
  Exceptions and :green/exit errors retain ordinary SDK behavior. Success is
  collected at :colors-compute/cluster, then optional downstream runs.
  This adapter supplies no lifecycle implementation itself."
  ([requests entry-node-id node-step]
   (cluster-workflow requests entry-node-id node-step nil))
  ([requests entry-node-id node-step downstream]
  (when (empty? requests) (throw (ex-info "no nodes requested" {})))
  (reduce (fn [seen request]
            (let [id (:node_id request)]
              (when (contains? seen id)
                (throw (ex-info (str "duplicate requested node: " id) {})))
              (conj seen id))) #{} requests)
  (compute/state-keys "validation" (map :node_id requests))
  (when-not (some #(= entry-node-id (:node_id %)) requests)
    (throw (ex-info (str "unknown entry node: " entry-node-id) {})))
  (when-not (ifn? node-step) (throw (ex-info "node-step must be callable" {})))
  (workflow/workflow
   {:start :colors-compute/dispatch
    :wire-fn
    (fn [step _]
      (case step
        :colors-compute/dispatch
        [(fn [opts] (dissoc opts :colors-compute/cluster :colors-compute/params :green/branches))
         :colors-compute/node]
        :colors-compute/node
        [(fn [opts]
           (let [result (node-step opts)
                 exit (get result :green/exit 0)]
             (if (or (and (number? exit) (zero? exit)) (and (integer? exit) (pos? exit)))
               result
               (assoc result :green/exit 1))))
         :colors-compute/join]
        :colors-compute/join
        (cond-> [(fn [opts]
           (let [branches (or (seq (:green/branches opts)) [opts])]
             (assoc opts :colors-compute/cluster
                    (compute/collect requests (vec (keep :colors-compute/params branches))
                                     entry-node-id))))]
          downstream (conj :colors-compute/downstream))
        :colors-compute/downstream [downstream]))
    :next-fn
    (fn [step successors opts]
      (cond
        (not= 0 (:green/exit opts 0)) []
        (= step :colors-compute/dispatch)
        (mapv (fn [request] [:colors-compute/node (assoc opts :colors-compute/request request)]) requests)
        :else (mapv (fn [next-step] [next-step opts]) successors)))})))
