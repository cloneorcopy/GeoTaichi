import numpy as np
import taichi as ti

from src.mpm.materials.ConstitutiveModelBase import ConstitutiveModelBase
from src.utils.MaterialKernel import *
from src.utils.constants import DELTA
from src.utils.MatrixFunction import matrix_form
from src.utils.ObjectIO import DictIO
from src.utils.TypeDefination import mat3x3
from src.utils.VectorFunction import voigt_form


class ElasticPerfectlyPlastic(ConstitutiveModelBase):
    def __init__(self, max_material_num, max_particle_num, configuration="ULMPM", solver_type="Explicit"):
        super().__init__()
        self.matProps = ElasticPerfectlyPlasticModel.field(shape=max_material_num)
        if configuration == "ULMPM":
            self.stateVars = ULStateVariable.field(shape=max_particle_num) 
        elif configuration == "TLMPM":
            self.stateVars = TLStateVariable.field(shape=max_particle_num) 

        if solver_type == "Implicit":
            self.stiffness_matrix = ti.Matrix.field(6, 6, float, shape=max_particle_num)

    def get_state_vars_dict(self, start_particle, end_particle):
        epstrain = np.ascontiguousarray(self.stateVars.epstrain.to_numpy()[start_particle:end_particle])
        estress = np.ascontiguousarray(self.stateVars.estress.to_numpy()[start_particle:end_particle])
        return {'epstrain': epstrain, 'estress': estress}
    
    def reload_state_variables(self, state_vars):
        estress = state_vars.item()['estress']
        epstrain = state_vars.item()['epstrain']
        kernel_reload_state_variables(estress, epstrain, self.stateVars)

    def model_initialize(self, material):
        materialID = DictIO.GetEssential(material, 'MaterialID') 
        self.check_materialID(materialID, self.matProps.shape[0])
        
        if self.matProps[materialID].density > 0.:
            print("Previous Material Property will be overwritten!")
        density = DictIO.GetAlternative(material, 'Density', 2650)
        young = DictIO.GetEssential(material, 'YoungModulus')
        possion = DictIO.GetAlternative(material, 'PossionRatio', 0.3)
        _yield = DictIO.GetEssential(material, 'YieldStress')

        self.matProps[materialID].add_material(density, young, possion, _yield)
        self.matProps[materialID].print_message(materialID)
    
    def get_lateral_coefficient(self, materialID):
        mu = self.matProps[materialID].possion
        return mu / (1. - mu)


@ti.dataclass
class ULStateVariable:
    epstrain: float
    estress: float

    @ti.func
    def _initialize_vars(self, np, particle, matProps):
        stress = particle[np].stress
        self.estress = VonMisesStress(stress)

    @ti.func
    def _update_vars(self, stress, epstrain):
        self.estress = VonMisesStress(stress)
        self.epstrain = epstrain

@ti.dataclass
class TLStateVariable:
    epstrain: float
    estress: float
    deformation_gradient: mat3x3
    stress: mat3x3

    @ti.func
    def _initialize_vars(self, np, particle, matProps):
        stress = particle[np].stress
        self.estress = VonMisesStress(stress)
        self.deformation_gradient = DELTA
        self.stress = matrix_form(stress)

    @ti.func
    def _update_deformation_gradient(self, deformation_gradient_rate, dt):
        self.deformation_gradient += deformation_gradient_rate * dt[None]

    @ti.func
    def _update_vars(self, stress, epstrain):
        self.estress = VonMisesStress(stress)
        self.epstrain = epstrain


@ti.dataclass
class ElasticPerfectlyPlasticModel:
    density: float
    young: float
    possion: float
    shear: float
    bulk: float
    _yield: float

    def add_material(self, density, young, possion, yield_stress):
        self.density = density
        self.young = young
        self.possion = possion

        self.shear = 0.5 * self.young / (1. + self.possion)
        self.bulk = self.young / (3. * (1 - 2. * self.possion))
        self._yield = yield_stress

    def print_message(self, materialID):
        print(" Constitutive Model Information ".center(71, '-'))
        print('Constitutive model: Elastic Perfectly Plastic Model')
        print("Model ID: ", materialID)
        print('Density: ', self.density)
        print('Young Modulus: ', self.young)
        print('Possion Ratio: ', self.possion)
        print('Yield Stress: ', self._yield, '\n')

    @ti.func
    def _get_sound_speed(self):
        sound_speed = 0.
        if self.density > 0.:
            sound_speed = ti.sqrt(self.young * (1 - self.possion) / (1 + self.possion) / (1 - 2 * self.possion) / self.density)
        return sound_speed
    
    @ti.func
    def update_particle_volume(self, np, velocity_gradient, stateVars, dt):
        return (DELTA + velocity_gradient * dt[None]).determinant()
    
    @ti.func
    def update_particle_volume_bbar(self, np, strain_rate, stateVars, dt):
        return 1. + dt[None] * (strain_rate[0] + strain_rate[1] + strain_rate[2])

    @ti.func
    def PK2CauchyStress(self, np, stateVars):
        inv_j = 1. / stateVars[np].deformation_gradient.determinant()
        return voigt_form(stateVars[np].stress @ stateVars[np].deformation_gradient.transpose() * inv_j)

    @ti.func
    def Cauchy2PKStress(self, np, stateVars, stress):
        j = stateVars[np].deformation_gradient.determinant()
        return matrix_form(stress) @ stateVars[np].deformation_gradient.inverse().transpose() * j
    
    @ti.func
    def ComputeStress2D(self, np, previous_stress, velocity_gradient, stateVars, dt):  
        de = calculate_strain_increment2D(velocity_gradient, dt)
        dw = calculate_vorticity_increment2D(velocity_gradient, dt)
        return self.core(np, previous_stress, de, dw, stateVars)

    @ti.func
    def ComputeStress(self, np, previous_stress, velocity_gradient, stateVars, dt):  
        de = calculate_strain_increment(velocity_gradient, dt)
        dw = calculate_vorticity_increment(velocity_gradient, dt)
        return self.core(np, previous_stress, de, dw, stateVars)
    
    @ti.func
    def core(self, np, previous_stress, de, dw, stateVars): 
        yield0 = self._yield
        shear_modulus, bulk_modulus = self.shear, self.bulk

        # !-- trial elastic stresses ----!
        stress = previous_stress
        sigrot = Sigrot(stress, dw)
        dstress = ElasticTensorMultiplyVector(de, bulk_modulus, shear_modulus)
        trial_stress = stress + dstress
        
        sigma = MeanStress(trial_stress)
        sd = DeviatoricStress(trial_stress)
        seqv = EquivalentStress(trial_stress)
        f_func = seqv - yield0

        depeff = 0.
        updated_stress = trial_stress
        if f_func > Threshold:
            depeff = (seqv - yield0) / (3. * self.shear)

            ratio = yield0 / seqv
            sd *= ratio
            seqv *= ratio

            updated_stress = AssembleStress(sigma, sd)

        updated_stress += sigrot
        stateVars[np].estress = VonMisesStress(updated_stress)
        stateVars[np].epstrain += depeff
        return updated_stress

    @ti.func
    def ComputePKStress(self, np, previous_stress, velocity_gradient, stateVars, dt):  
        previous_stress = self.PK2CauchyStress(np, stateVars, previous_stress)
        stress = self.ComputeStress(np, previous_stress, velocity_gradient, stateVars, dt)
        return self.Cauchy2PKStress(np, stateVars, stress)
    
    @ti.func
    def compute_elastic_tensor(self, np, current_stress, stiffness, stateVars):
        ComputeElasticStiffnessTensor(np, self.bulk, self.shear, stiffness)

    @ti.func
    def compute_stiffness_tensor(self, np, current_stress, stiffness, stateVars):
        pass


@ti.kernel
def kernel_reload_state_variables(estress: ti.types.ndarray(), epstrain: ti.types.ndarray(), state_vars: ti.template()):
    for np in range(estress.shape[0]):
        state_vars[np].estress = estress[np]
        state_vars[np].epstrain = epstrain[np]