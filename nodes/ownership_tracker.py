#!/usr/bin/env python
import rospy
import numpy as np
from sklearn.kernel_approximation import Nystroem
from sklearn.linear_model import LogisticRegression
from std_srvs.srv import *
from ownage_bot import *
from ownage_bot.msg import *
from ownage_bot.srv import *
from object_tracker import ObjectTracker

class OwnershipTracker(ObjectTracker):
    """Tracks ownership based on physical and social observation."""
    
    def __init__(self):
        super(OwnershipTracker, self).__init__()

        # Flag to disable rule-based inference
        self.disable_inference =\
            rospy.get_param("~disable_inference", False)
        # Flag to disable percept-based extrapolation
        self.disable_extrapolate =\
            rospy.get_param("~disable_extrapolate", False)
        
        # Set up callback to handle ownership claims
        self.owner_sub = rospy.Subscriber("owner_input", PredicateMsg,
                                          self.ownerClaimCb)
        # Set up callback to predict ownership upon new permission input
        self.perm_sub = rospy.Subscriber("perm_input", PredicateMsg,
                                         self.permInputCb)
        # Set up callback to predict ownership upon new object detection
        self.new_agt_sub = rospy.Subscriber("new_agent", AgentMsg,
                                            self.newAgentCb)

        # Services to stop owner prediction
        self.dis_infer_srv = rospy.Service("disable_inference", SetBool,
                                           self.disableInferenceCb)
        self.dis_extra_srv = rospy.Service("disable_extrapolate", SetBool,
                                           self.disableExtrapolateCb)
        
        # Client for looking up active rules
        self.lookupRules = rospy.ServiceProxy("lookup_rules", LookupRules)
        
        # How much to trust ownership claims
        self.claim_trust = rospy.get_param("~claim_trust", 1.0)
        
        # Logistic regression params and objects for percept-based prediction
        self.reg_strength = rospy.get_param("~reg_strength", 0.1)
        self.max_features = rospy.get_param("~max_features", 20)
        self.nys = Nystroem(kernel='precomputed', random_state=0)
        self.log_reg = LogisticRegression(C=1/self.reg_strength,
                                          solver='newton-cg')

    def disableInferenceCb(self, req):
        """Disables rule-based ownership inference."""
        self.disable_inference = req.data
        return SetBoolResponse(True, "")

    def disableExtrapolateCb(self, req):
        """Disables percept-based ownership extrapolation."""
        self.disable_extrapolate = req.data
        return SetBoolResponse(True, "")
        
    def ownerClaimCb(self, msg):
        """Callback upon receiving claim of ownership about object."""
        # Unpack object and owner identity from isOwned predicate
        pred = Predicate.fromMsg(msg)
        obj = pred.bindings[0]
        agent = pred.bindings[1]
        if pred.name != predicates.OwnedBy.name:
            return
        if not isinstance(obj, Object):
            return
        if not isinstance(agent, Agent):
            # TODO: Handle non-specific and group ownership claims
            return

        # Compute ownership probability as product of trust and truth value
        p_owned = self.claim_trust * msg.truth
        if pred.negated:
            p_owned = 1 - p_owned
        self.object_db[obj.id].ownership[agent.id] = p_owned
        
    def permInputCb(self, msg):
        """Callback upon receiving permission information about objects."""
        # Do nothing if inference is disabled
        if self.disable_inference:
            return
        
        # Ignore perms which are not about actions
        if msg.predicate not in actions.db:
            return
        action = actions.db[msg.predicate]

        # Ignore actions without objects as targets
        if len(msg.bindings) != 1:
            raise TypeError("Action perm should have exactly one argument.")
        if (msg.bindings[0] == objects.Nil.toStr() or action.tgtype != Object):
            raise TypeError("Action perm should have object as argument.")
        obj = Object.fromStr(msg.bindings[0])
        
        # Guess ownership and publish prediction
        ownership = self.inferFromPerm(action.name, obj, msg.truth)
        print ownership
        self.object_db[obj.id].ownership = ownership

    def newAgentCb(self, msg):
        """Callback upon new agent introduction."""
        # Default ownership probability to 0.5
        for o_id in self.object_db.keys():
            self.object_db[o_id].ownership[msg.id] = 0.5
        
    def inferFromPerm(self, act_name, obj, truth):
        """Infer ownership from permission info."""
        rule_set = self.lookupRules(act_name).rule_set
        rule_set = [Rule.fromMsg(r) for r in rule_set]

        # Do Bayesian update using potential explanations
        p_owned_prior = obj.ownership
        p_owned_post = dict()
        p_f_owned = dict()
        p_a_owned = dict()
        p_forbidden = Rule.evaluateOr(rule_set, obj)
        p_allowed = 1 - p_forbidden

        for a in Agent.universe():
            # Suppose that obj is owned by agent a
            obj.ownership = dict(p_owned_prior)
            obj.ownership[a.id] = 1.0
            
            # Find P(forbidden|owned by a) and P(allowed|owned by a)
            p_f_cond = Rule.evaluateOr(rule_set, obj)
            p_a_cond = 1 - p_f_cond
            
            # Find P(forbidden & owned by a) and P(allowed & owned by a)
            p_f_owned[a.id] = p_f_cond *  p_owned_prior[a.id]
            p_a_owned[a.id] = p_a_cond *  p_owned_prior[a.id]
            
        # Compute updated probability of ownership
        for a in Agent.universe():
            # P(owned by a|perm) =
            # P(owned by a|forbidden) P(forbidden|perm) +
            # P(owned by a|allowed) P(allowed|perm)
            p_owned_post[a.id] = 0
            if p_forbidden > 0:
                p_owned_post[a.id] += p_f_owned[a.id] / p_forbidden * truth
            if p_allowed > 0:
                p_owned_post[a.id] += p_a_owned[a.id] / p_allowed * (1-truth)

        # Reset ownership to original
        obj.ownership = p_owned_prior

        # Return posterior probabilities
        return p_owned_post

    def extrapolateFromPercepts(self, new):
        """Guess ownership of new object from physical percepts."""
        # Compute Gram matrix and kernel map for kernel logistic regression
        K = self.perceptKern(objs, objs)
        self.nys.n_components = min(len(objs), self.max_features)
        X = self.nys.fit_transform(K)
        
        # Duplicate samples to account for uncertainty in class labels
        X = np.tile(X, [2,1])
        y = [True] * len(objs) + [False] * len(objs)
        
        # Train the classifier for each possible owner and predict ownership
        ownership = dict()
        for a in Agent.universe():
            # Weight samples according to the certainty of ownership
            weights = np.array([o.ownership[a.id] for o in objs] +
                               [1.0-o.ownership[a.id] for o in objs])
            self.log_reg.fit(X, y, sample_weight=weights)

            # Predict ownership of new object
            K_new = self.perceptKern([new], objs)
            X_new = self.nys.transform(K_new)
            probs = self.log_reg.predict_proba(X_new)
            ownership[a.id] = probs[0,1]
        
        return ownership

    def perceptDiff(self, o1, o2):
        """Computes raw displacement in perceptual space between objects."""
        col_diff = 1.0 if o1.color != o2.color else 0.0
        p1, p2 = o1.position, o2.position
        pos_diff = [p1.x-p2.x, p1.y-p2.y, p1.z-p2.z]
        return np.array([col_diff] + pos_diff)

    def perceptKern(self, objs1, objs2, gamma=1.0):
        """Computes RBF kernel matrix for the percept features of objects."""
        diffs = [[self.perceptDiff(o1, o2) for o2 in objs2] for o1 in objs1]
        sq_dists = np.array([[np.dot(d, d) for d in row] for row in diffs])
        return np.exp(-gamma * sq_dists)        

if __name__ == '__main__':
    rospy.init_node('ownership_tracker')
    OwnershipTracker()
    rospy.spin()