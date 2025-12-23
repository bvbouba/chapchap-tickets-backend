from graphene import ObjectType, String, Boolean


class PhoneExistenceType(ObjectType):
    exists = Boolean()
    error = String()
