"""
Python implementation of the generalized PCA for dimension reduction of non-normally distributed data. The original R implementation is at https://github.com/willtownes/glmpca
"""
import numpy as np
from numpy import log
from scipy.special import digamma,polygamma
import statsmodels.api as sm
import statsmodels.genmod.families as smf
from decimal import Decimal

def trigamma(x):
    return polygamma(1,x)

def rowSums(x):
    return x.sum(axis=1)

def rowMeans(x):
    return x.mean(axis=1)

def colSums(x):
    return x.sum(axis=0)

def colMeans(x):
    return x.mean(axis=0)

def colNorms(x):
    """
    compute the L2 norms of columns of an array
    """
    return np.sqrt(colSums(x**2))

def ncol(x):
    return x.shape[1]

def nrow(x):
    return x.shape[0]

def crossprod(A,B):
    return (A.T)@B

def tcrossprod(A,B):
    return A@(B.T)

def cvec1(n):
    """returns a column vector of ones with length N"""
    return np.ones((n,1))

def remove_intercept(X):
    cm = colMeans(X)
    try:
        X-= cm
    except TypeError as err:
        if X.dtype != cm.dtype:
            X = X.astype(cm.dtype) - cm
        else:
            raise err
    return X[:,colNorms(X)>1e-12]


class GlmpcaError(ValueError):
  pass


class GlmpcaFamily(object):
    """thin wrapper around the statsmodels.genmod.families.Family class"""
    # TO DO: would it be better to use inheritance?
    def __init__(self, glm_family=None, nb_theta=None):

        assert glm_family in ["poi","nb","mult","bern"], 'Invalid GLM family'
        self.glm_family = glm_family
        self.nb_theta = nb_theta

        if self.glm_family == "poi":
            self.family=smf.Poisson()

        elif self.glm_family == "nb":
            if self.nb_theta is None:
                raise GlmpcaError("Negative binomial dispersion parameter 'nb_theta' must be specified")
            self.family= smf.NegativeBinomial(alpha=1/nb_theta)

        elif self.glm_family in ("mult","bern"):
            self.family = smf.Binomial()

        else:
            raise GlmpcaError("unrecognized family type")

        #variance function, determined by GLM family
        self.vfunc= self.family.variance
        #inverse link func, mu as a function of linear predictor R
        self.ilfunc= self.family.link.inverse
        #derivative of inverse link function, dmu/dR
        self.hfunc= self.family.link.inverse_deriv

        # define variables define later
        self.rfunc = None
        self.offset = None
        self.multi_n = None
        self.nb_theta = None
        self.intercepts = None
        self.sz = None


    def initialize(self, Y, sz = None):
        """
        create the glmpca_family object and
        initialize the A array (regression coefficients of X)
        Y is the data (JxN array)
        fam is the likelihood
        sz optional vector of size factors, default: sz=colMeans(Y) or colSums(Y)
        sz is ignored unless fam is 'poi' or 'nb'
        """
        self.mult_n = colSums(Y) if self.glm_family == "mult" else None
        if self.glm_family == "mult" and self.mult_n is None:
            raise GlmpcaError("Multinomial sample size parameter vector 'mult_n' must be specified")

        if self.glm_family in ("poi","nb"):
            self.sz = colMeans(Y) if sz is not None else sz
            self.offsets = self.family.link(self.sz)
            self.rfunc = lambda U,V: self.offsets + tcrossprod(V,U) #linear predictor
            self.intercepts = self.family.link(rowSums(Y)/np.sum(self.sz))

        else:
            self.offsets = 0
            self.rfunc= lambda U,V: tcrossprod(V,U)
            if self.glm_family=="mult": #offsets incorporated via family object
                self.intercepts = self.family.link(rowSums(Y)/np.sum(self.mult_n))
            else: #no offsets (eg, bernoulli)
                self.intercepts = self.family.link(rowMeans(Y))

        if np.any(np.isinf(self.intercepts)):
            raise GlmpcaError("Some rows were all zero, please remove them.")



    def infograd(self, Y, R):
        if self.glm_family == "poi":
            M = self.ilfunc(R) #ilfunc=exp
            return {"grad":(Y-M),"info":M}

        elif self.glm_family == "nb":
            M = self.ilfunc(R) #ilfunc=exp
            W = 1/self.vfunc(M)
            return {"grad":(Y-M)*W*M,"info":W*(M**2)}

        elif self.glm_family == "mult":
            P = self.ilfunc(R) #ilfunc=expit, P very small probabilities
            return {"grad":Y-(self.mult_n*P),"info":self.mult_n*self.vfunc(P)}

        elif self.glm_family == "bern":
            P = self.ilfunc(R)
            return {"grad":Y-P,"info":self.vfunc(P)}

        else: #this is not actually used but keeping for future reference
            #this is most generic formula for GLM but computationally slow
            raise GlmpcaError("invalid fam")
            M= self.ilfunc(R)
            W= 1/self.vfunc(M)
            H= self.hfunc(R)
            return {"grad":(Y-M)*W*H,"info":W*(H**2)}


    def dev_func(self, Y, R):
        #create deviance function
        if self.glm_family == "mult":
            return self.mat_binom_dev(Y,self.ilfunc(R),self.mult_n)
        else:
            return self.family.deviance(Y,self.ilfunc(R))


    def __str__(self):
        return "GlmpcaFamily object of type {}".format(self.family)


    def mat_binom_dev(self, X,P,n):
        """
        binomial deviance for two arrays
        X,P are JxN arrays
        n is vector of length N (same as cols of X,P)
        """
        with np.errstate(divide='ignore',invalid='ignore'):
            term1= X*log(X/(n*P))
        term1= term1[np.isfinite(term1)].sum()
        #nn= x<n
        nx= n-X
        with np.errstate(divide='ignore',invalid='ignore'):
            term2= nx*log(nx/(n*(1-P)))
        term2= term2[np.isfinite(term2)].sum()
        return 2*(term1+term2)


def est_nb_theta(y,mu,th):
  """
  given count data y and predicted means mu>0, and a neg binom theta "th"
  use Newton's Method to update theta based on the negative binomial likelihood
  note this uses observed rather than expected information
  regularization:
  let u=log(theta). We use the prior u~N(0,1) as penalty
  equivalently we assume theta~lognormal(0,1) so the mode is at 1 (geometric distr)
  dtheta/du=e^u=theta
  d2theta/du2=theta
  dL/dtheta * dtheta/du
  """
  #n= length(y)
  u= log(th)
  #dL/dtheta*dtheta/du
  score=  th*np.sum(digamma(th+y)-digamma(th)+log(th)+1-log(th+mu)-(y+th)/(mu+th))
  #d^2L/dtheta^2 * (dtheta/du)^2
  info1=  -(th**2)*np.sum(trigamma(th+mu)-trigamma(th)+1/th-2/(mu+th)+(y+th)/(mu+th)**2)
  #dL/dtheta*d^2theta/du^2 = score
  info=  info1-score
  #L2 penalty on u=log(th)
  return np.exp(u+(score-u)/(info+1))
  #grad= score-u
  #exp(u+sign(grad)*min(maxstep,abs(grad)))


class GlmPCA():
    
    def __init__(self, n_components=1, family="poi", maxiter = 1000,eps=1e-4,
            penalty = 1, verbose = False, init = {"factors": None, "loadings":None},
            nb_theta = 100):

        """
        GLM-PCA

        This function implements the GLM-PCA dimensionality reduction method for high-dimensional count data.

        The basic model is R = AX'+ZG'+VU', where E[Y]=M=linkinv(R). Regression coefficients are A and G, latent factors are U, and loadings are V. The objective function being optimized is the deviance between Y and M, plus an L2 (ridge) penalty on U and V. Note that glmpca uses a random initialization, so for fully reproducible results one should set the random seed.

        Parameters
        ----------
        n_components: the desired number of latent dimensions (integer).
        family: string describing the likelihood to use for the data. Possible values include:
        - poi: Poisson
        - nb: negative binomial
        - mult: binomial approximation to multinomial
        - bern: Bernoulli
        maxiter: Maximum number of iterations to perform.
        eps: Convergence criterion for the mode-finding procedure. The algorithm is considered to have converged if the relative differences in all parameters from one iteration to the next are less than eps--that is, if all(abs(new-old)<eps*abs(old)).
        penalty: the L2 penalty for the latent factors (default = 1).
            Regression coefficients are not penalized.
        verbose: logical value indicating whether the current deviance should
            be printed after each iteration (default = False).
        init: a dictionary containing initial estimates for the factors (U) and
            loadings (V) matrices.
        nb_theta: negative binomial dispersion parameter. Smaller values mean more dispersion
            if nb_theta goes to infinity, this is equivalent to Poisson
            Note that the alpha in the statsmodels package is 1/nb_theta.

        Y: array_like of count data with features as rows and observations as
            columns.
        X: array_like of column (observations) covariates. Any column with all
            same values (eg. 1 for intercept) will be removed. This is because we force
            the intercept and want to avoid collinearity.
        Z: array_like of row (feature) covariates, usually not needed.
        sz: numeric vector of size factors to use in place of total counts.

        Returns
        -------
        A dictionary with the following elements
        - factors: an array U whose rows match the columns (observations) of Y. It is analogous to the principal components in PCA. Each column of the factors array is a different latent dimension.
        - loadings: an array V whose rows match the rows (features/dimensions) of Y. It is analogous to loadings in PCA. Each column of the loadings array is a different latent dimension.
        - coefX: an array A of coefficients for the observation-specific covariates array X. Each row of coefX corresponds to a row of Y and each column corresponds to a column of X. The first column of coefX contains feature-specific intercepts which are included by default.
        - coefZ: a array G of coefficients for the feature-specific covariates array Z. Each row of coefZ corresponds to a column of Y and each column corresponds to a column of Z. By default no such covariates are included and this is returned as None.
        - dev: a vector of deviance values. The length of the vector is the number of iterations it took for GLM-PCA's optimizer to converge. The deviance should generally decrease over time. If it fluctuates wildly, this often indicates numerical instability, which can be improved by increasing the penalty parameter.
        - glmpca_family: an object of class GlmpcaFamily. This is a minor wrapper to the family object used by the statsmodels package for fitting standard GLMs. It contains various internal functions and parameters needed to optimize the GLM-PCA objective function. For the negative binomial case, it also contains the final estimated value of the dispersion parameter nb_theta.

        Examples
        -------
        1) create a simple dataset with two clusters and visualize the latent structure
        >>> from numpy import array,exp,random,repeat
        >>> from matplotlib.pyplot import scatter
        >>> from glmpca import GlmPCA
        >>> mu= exp(random.randn(20,100))
        >>> mu[range(10),:] *= exp(random.randn(100))
        >>> clust= repeat(["red","black"],10)
        >>> Y= random.poisson(mu)
        >>> res= glmpca(Y.T, 2)
        >>> factors= res["factors"]
        >>> scatter(factors[:,0],factors[:,1],c=clust)

        References
        ----------
        .. [1] Townes FW, Hicks SC, Aryee MJ, and Irizarry RA. "Feature selection and dimension reduction for single-cell RNA-seq based on a multinomial model", biorXiv, 2019. https://www.biorxiv.org/content/10.1101/574574v1
        .. [2] Townes FW. "Generalized principal component analysis", arXiv, 2019. https://arxiv.org/abs/1907.02647

        """
        #For negative binomial, convergence only works if starting with nb_theta large

        if family not in {"poi","nb","mult","bern"}:
            raise GlmpcaError('Invalid GLM family')
        self.n_components = n_components
        self.family = family
        self.maxiter = maxiter
        self.eps = eps
        self.penalty = penalty
        self.verbose = verbose 
        self.init = {"factors": None, "loadings":None}
        self.nb_theta = nb_theta
        self.gf = GlmpcaFamily(glm_family=family, nb_theta=self.nb_theta)
        self.sz = None


    def ortho(self, U, V, A, X=1, G=None, Z=0):
        """
        U is NxL array of cell factors
        V is JxL array of loadings onto genes
        X is NxKo array of cell specific covariates
        A is JxKo array of coefficients of X
        Z is JxKf array of gene specific covariates
        G is NxKf array of coefficients of Z
        assume the data Y is of dimension JxN
        imputed expression: E[Y] = g^{-1}(R) where R = VU'+AX'+ZG'
        """
        if np.all(X==1): 
            X = cvec1(nrow(U))

        if np.all(Z==0): 
            Z = np.zeros((nrow(V),1))

        L= ncol(U)
        if np.all(G==0): 
            G= None
        #we assume A is not null or zero
        #remove correlation between U and A
        #at minimum, this will cause factors to have mean zero
        betax = np.linalg.lstsq(X,U,rcond=None)[0] #extract coef from linreg
        factors = U-X@betax #residuals from linear regression
        A += tcrossprod(V,betax)
        #remove correlation between V and G
        if G is None:
            loadings= V
        else: #G is not empty
            betaz = np.linalg.lstsq(Z,V,rcond=None)[0] #extract coef from linreg
            loadings = V-Z@betaz #residuals from regression
            G += tcrossprod(factors,betaz)
        #rotate factors to make loadings orthornormal
        loadings,d,Qt = np.linalg.svd(loadings,full_matrices=False)
        factors = tcrossprod(factors,Qt)*d #d vector broadcasts across cols
        #arrange latent dimensions in decreasing L2 norm
        o = (-colNorms(factors)).argsort()
        self.factors = factors[:,o]
        self.loadings = loadings[:,o]
        self.coefX = A
        self.coefZ = G


    def fit(self, Y, X=None, Z=None, sz=None):
        Y = np.array(Y)
        J,N = Y.shape

        if np.min(Y)<0:
            raise GlmpcaError("for count data, the minimum value must be >=0")

        if np.any(np.max(Y, axis=1) == 0 ): #matching R version glmpca
            raise GlmpcaError('Some rows were all zero, please remove them.')
        
        if self.family=="bern" and np.max(Y)>1:
            raise GlmpcaError("for Bernoulli model, the maximum value must be <=1")

        #preprocess covariates and set updateable indices
        if X is not None:
            if nrow(X) != ncol(Y):
                raise GlmpcaError("X rows must match columns of Y")
            #we force an intercept, so remove it from X to prevent collinearity
            X= remove_intercept(X)
        else:
            X= np.zeros((N,0)) #empty array to prevent dim mismatch errors with hstack later
        Ko= ncol(X)+1

        if Z is not None:
            if nrow(Z) != nrow(Y):
                raise GlmpcaError("Z rows must match rows of Y")
        else:
            Z = np.zeros((J,0)) #empty array to prevent dim mismatch errors with hstack later
        Kf= ncol(Z)


        if sz is not None and len(sz) != ncol(Y):
            raise GlmpcaError("size factor must have length equal to columns of Y")

        lid= (Ko + Kf)+np.array(range(self.n_components))
        uid= Ko + np.array(range(Kf+self.n_components))
        vid= np.concatenate((np.array(range(Ko)),lid))
        Ku= len(uid)
        Kv= len(vid)

        self.gf.initialize(Y, sz=sz)
        
        #initialize U,V, with row-specific intercept terms
        U= np.hstack((cvec1(N), X, np.random.randn(N,Ku)*1e-5/Ku))
        if self.init["factors"] is not None:
            L0= np.min([self.n_components,ncol(self.init["factors"])])
            U[:,(Ko+Kf)+np.array(range(L0))]= self.init["factors"][:,range(L0)]
        #a1 = naive MLE for gene intercept only, must convert to column vector first with [:,None]
        V= np.hstack((self.gf.intercepts[:,None], np.random.randn(J,(Ko-1))*1e-5/Kv))
        #note in the above line the randn can be an empty array if Ko=1, which is OK!
        V= np.hstack((V, Z, np.random.randn(J, self.n_components)*1e-5/Kv))
        if self.init["loadings"] is not None:
            L0= np.min([self.n_components,ncol(self.init["loadings"])])
            V[:,(Ko+Kf)+np.array(range(L0))] = self.init["loadings"][:,range(L0)]

        #run optimization
        dev = np.repeat(np.nan,self.maxiter)
        for t in range(self.maxiter):
            dev[t]= self.gf.dev_func(Y, self.gf.rfunc(U,V))
            if not np.isfinite(dev[t]):
                raise GlmpcaError("Numerical divergence (deviance no longer finite), '\
                                'try increasing the penalty to improve stability of optimization.")
            if t>4 and np.abs(dev[t]-dev[t-1])/(0.1+np.abs(dev[t-1]))<self.eps:
                break
            if self.verbose:
                msg = "Iteration: {:d} | deviance={:.4E}".format(t,Decimal(dev[t]))
                if self.family == "nb": 
                    msg += " | nb_theta: {:.3E}".format(self.nb_theta)
                print(msg)

            #(k in lid) ensures no penalty on regression coefficients:
            for k in vid:
                ig = self.gf.infograd(Y, self.gf.rfunc(U,V))
                grads = ig["grad"] @ U[:,k] - self.penalty * V[:,k] * (k in lid)
                infos= ig["info"] @ (U[:,k]**2) + self.penalty * (k in lid)
                V[:,k] += grads/infos

            for k in uid:
                ig = self.gf.infograd(Y, self.gf.rfunc(U,V))
                grads = crossprod(ig["grad"], V[:,k]) - self.penalty*U[:,k]*(k in lid)
                infos = crossprod(ig["info"], V[:,k]**2) + self.penalty*(k in lid)
                U[:,k] += grads/infos

            if self.family == "nb":
                self.nb_theta = est_nb_theta(Y, 
                        self.gf.family.link.inverse(self.gf.rfunc(U,V)), self.nb_theta)
                self.gf = GlmpcaFamily(glm_family = self.family, nb_theta = self.nb_theta)
                self.gf.initialize(Y, sz=sz)
        #postprocessing: include row and column labels for regression coefficients
        G = None if ncol(Z)==0 else U[:,Ko+np.array(range(Kf))]
        X = np.hstack((cvec1(N),X))
        A = V[:,range(Ko)]
        self.ortho(U[:,lid],V[:,lid],A,X=X,G=G,Z=Z)
        self.dev = dev[range(t+1)]



if __name__=="__main__":
    from numpy import array,exp,random,repeat
    np.random.seed(1)
    mu= exp(random.randn(20,100))
    mu[range(10),:] *= exp(random.randn(100))
    clust= repeat(["red","black"],10)
    Y= random.poisson(mu)
    glmpca = GlmPCA(n_components=2, family='nb', verbose=True) 
    glmpca.fit(Y.T)
    print(glmpca.factors)
    print(glmpca.dev)
    #from matplotlib.pyplot import scatter
    #%pylab
    #scatter(factors[:,0],factors[:,1],c=clust)